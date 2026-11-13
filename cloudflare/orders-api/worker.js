const CORS={"Access-Control-Allow-Origin":"*","Access-Control-Allow-Methods":"GET,POST,OPTIONS","Access-Control-Allow-Headers":"Content-Type,Authorization,Idempotency-Key","Vary":"Origin"};
const json=(data,status=200,extra={})=>new Response(JSON.stringify(data),{status,headers:{...CORS,"Content-Type":"application/json; charset=utf-8","Cache-Control":"no-store",...extra}});
const now=()=>new Date().toISOString(), uuid=()=>crypto.randomUUID();
const auth=(req,token)=>Boolean(token)&&req.headers.get("Authorization")==="Bearer "+token;
const units=v=>{const s=String(v??"").trim();if(!/^\d+(\.\d{1,4})?$/.test(s))return null;const [w,f=""]=s.split(".");const n=Number(w)*10000+Number((f+"0000").slice(0,4));return Number.isSafeInteger(n)&&n>0?n:null;};
const money=n=>Math.floor(n/10000)+"."+String(n%10000).padStart(4,"0");
const safeOrder=o=>({order_id:o.order_id,supplier_product_id:o.supplier_product_id,product_title:o.product_title,quantity:o.quantity,unit_price_usdt:o.unit_price_usdt,amount_usdt:o.amount_usdt,currency:"USDT",payment_status:o.payment_status,procurement_status:o.procurement_status,delivery_status:o.delivery_status,created_at:o.created_at,expires_at:o.expires_at,paid_at:o.paid_at,updated_at:o.updated_at,error_code:o.error_code});
const getOrder=(env,id)=>env.DB.prepare("SELECT * FROM faka_orders WHERE order_id=?").bind(id).first();
async function audit(env,event,orderId,details={}){await env.DB.prepare("INSERT INTO faka_order_audit (id,event,order_id,details_json,created_at) VALUES (?,?,?,?,?)").bind(uuid(),event,orderId,JSON.stringify(details),now()).run();}
async function createOrder(req,env){
 if(!auth(req,env.BOT_API_TOKEN))return json({ok:false,error:"unauthorized"},401);
 const idem=req.headers.get("Idempotency-Key");if(!idem||idem.length<12||idem.length>128)return json({ok:false,error:"idempotency_key_required"},400);
 let b;try{b=await req.json()}catch{return json({ok:false,error:"invalid_json"},400)}
 const user=String(b.telegram_user_id||"").trim(),productId=String(b.supplier_product_id||"").trim(),qty=Number(b.quantity??1);
 if(!/^\d{1,32}$/.test(user)||!productId||productId.length>80||!Number.isInteger(qty)||qty<1||qty>10)return json({ok:false,error:"invalid_order_input"},400);
 const prior=await env.DB.prepare("SELECT * FROM faka_orders WHERE idempotency_key=?").bind(idem).first();
 if(prior){if(prior.telegram_user_id!==user)return json({ok:false,error:"idempotency_key_conflict"},409);return json({ok:true,replayed:true,order:safeOrder(prior)})}
 const state=await env.DB.prepare("SELECT synced_at FROM catalog_sync_state WHERE id=1").first();if(!state?.synced_at)return json({ok:false,error:"catalog_not_ready"},503);
 const p=await env.DB.prepare("SELECT supplier_product_id,title,sell_price_usdt,stock_status,raw_json FROM catalog_snapshot_products WHERE version=? AND supplier_product_id=? AND active=1").bind(state.synced_at,productId).first();
 if(!p)return json({ok:false,error:"product_unavailable"},409);if(p.stock_status!=="in_stock")return json({ok:false,error:"product_out_of_stock"},409);
 let raw={};try{raw=JSON.parse(p.raw_json||"{}")}catch{}
 const stock=Number(raw.InStock??raw.stock??0),min=Number(raw.MinBuy||0),max=Number(raw.MaxBuy||0);
 if(!Number.isFinite(stock)||stock<qty)return json({ok:false,error:"insufficient_supplier_stock"},409);
 if((min>0&&qty<min)||(max>0&&qty>max))return json({ok:false,error:"quantity_out_of_range"},400);
 const unit=units(Number(p.sell_price_usdt).toFixed(4));if(!unit)return json({ok:false,error:"invalid_catalog_price"},503);
 const total=unit*qty;if(!Number.isSafeInteger(total))return json({ok:false,error:"amount_overflow"},400);
 const created=now(),expires=new Date(Date.now()+20*60*1000).toISOString(),orderId=uuid();
 try{await env.DB.prepare("INSERT INTO faka_orders (order_id,idempotency_key,telegram_user_id,supplier_product_id,product_title,quantity,catalog_version,unit_price_usdt,amount_usdt,payment_status,procurement_status,delivery_status,created_at,expires_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,'pending','not_started','not_ready',?,?,?)").bind(orderId,idem,user,productId,p.title,qty,state.synced_at,money(unit),money(total),created,expires,created).run();}
 catch(e){const raced=await env.DB.prepare("SELECT * FROM faka_orders WHERE idempotency_key=?").bind(idem).first();if(raced&&raced.telegram_user_id===user)return json({ok:true,replayed:true,order:safeOrder(raced)});return json({ok:false,error:"order_create_conflict"},409)}
 await audit(env,"order_created",orderId,{product_id:productId,quantity:qty,catalog_version:state.synced_at});
 return json({ok:true,replayed:false,order:safeOrder(await getOrder(env,orderId))},201);
}
async function paymentEvent(req,env){
 if(!auth(req,env.PAYMENT_LISTENER_TOKEN))return json({ok:false,error:"unauthorized"},401);
 let b;try{b=await req.json()}catch{return json({ok:false,error:"invalid_json"},400)}
 const eventId=String(b.event_id||"").trim(),orderId=String(b.order_id||"").trim(),provider=String(b.provider||"").trim(),paymentRef=String(b.payment_ref||"").trim(),currency=String(b.currency||"").toUpperCase(),amount=units(b.amount_usdt);
 if(!eventId||eventId.length>160||!orderId||!provider||!paymentRef||paymentRef.length>200||currency!=="USDT"||!amount||b.verified!==true)return json({ok:false,error:"invalid_or_unverified_payment_event"},400);
 const order=await getOrder(env,orderId);if(!order)return json({ok:false,error:"order_not_found"},404);
 if(order.payment_status==="paid")return json({ok:true,replayed:true,order:safeOrder(order)});
 if(order.payment_status!=="pending")return json({ok:false,error:"order_not_payable",payment_status:order.payment_status},409);
 if(Date.parse(order.expires_at)<=Date.now()){await env.DB.prepare("UPDATE faka_orders SET payment_status='expired',updated_at=? WHERE order_id=? AND payment_status='pending'").bind(now(),orderId).run();return json({ok:false,error:"order_expired"},410)}
 if(amount!==units(order.amount_usdt))return json({ok:false,error:"payment_amount_mismatch",expected:order.amount_usdt,received:money(amount)},409);
 const received=now();
 try{await env.DB.batch([
  env.DB.prepare("INSERT OR IGNORE INTO faka_payment_events (event_id,order_id,provider,payment_ref,amount_usdt,currency,payload_hash,received_at) VALUES (?,?,?,?,?,'USDT',?,?)").bind(eventId,orderId,provider,paymentRef,money(amount),String(b.payload_hash||""),received),
  env.DB.prepare("UPDATE faka_orders SET payment_status='paid',payment_provider=?,payment_ref=?,paid_at=?,procurement_status='queued',updated_at=? WHERE order_id=? AND payment_status='pending' AND amount_usdt=? AND EXISTS (SELECT 1 FROM faka_payment_events WHERE event_id=? AND order_id=? AND payment_ref=? AND amount_usdt=?)").bind(provider,paymentRef,received,received,orderId,money(amount),eventId,orderId,paymentRef,money(amount)),
  env.DB.prepare("INSERT OR IGNORE INTO faka_procurement_tasks (task_id,order_id,status,created_at,updated_at) SELECT ?,order_id,'queued',?,? FROM faka_orders WHERE order_id=? AND payment_status='paid'").bind(uuid(),received,received,orderId)
 ])}catch{return json({ok:false,error:"payment_record_failed"},503)}
 const updated=await getOrder(env,orderId);if(updated?.payment_status!=="paid")return json({ok:false,error:"payment_event_conflict"},409);
 await audit(env,"payment_verified",orderId,{provider,payment_ref:paymentRef,event_id:eventId,amount_usdt:money(amount)});
 return json({ok:true,replayed:false,order:safeOrder(updated)},202);
}
export default {async fetch(req,env){
 const url=new URL(req.url),path=url.pathname;if(req.method==="OPTIONS")return new Response(null,{status:204,headers:CORS});
 if(path==="/health"&&req.method==="GET"){try{const [catalog,counts]=await Promise.all([env.DB.prepare("SELECT synced_at,product_count,category_count FROM catalog_sync_state WHERE id=1").first(),env.DB.prepare("SELECT payment_status,procurement_status,delivery_status,COUNT(*) AS count FROM faka_orders GROUP BY payment_status,procurement_status,delivery_status").all()]);return json({ok:true,service:"fakabot-orders",database:"connected",catalog,order_counts:counts.results||[],procurement_enabled:env.PROCUREMENT_ENABLED==="true",procurement_mode:env.PROCUREMENT_ENABLED==="true"?"armed":"paused"})}catch{return json({ok:false,error:"database_unavailable"},503)}}
 if(path==="/v1/orders"&&req.method==="POST")return createOrder(req,env);
 if(path==="/v1/internal/payment-events"&&req.method==="POST")return paymentEvent(req,env);
 const orderMatch=path.match(/^\/v1\/orders\/([a-f0-9-]{36})$/i);
 if(orderMatch&&req.method==="GET"){if(!auth(req,env.BOT_API_TOKEN))return json({ok:false,error:"unauthorized"},401);const user=String(url.searchParams.get("telegram_user_id")||""),o=await getOrder(env,orderMatch[1]);if(!o||o.telegram_user_id!==user)return json({ok:false,error:"order_not_found"},404);return json({ok:true,order:safeOrder(o)})}
 if(path==="/v1/internal/procurement-jobs"&&req.method==="GET"){if(!auth(req,env.PROCUREMENT_EXECUTOR_TOKEN))return json({ok:false,error:"unauthorized"},401);if(env.PROCUREMENT_ENABLED!=="true")return json({ok:false,error:"procurement_paused"},423);const {results=[]}=await env.DB.prepare("SELECT t.task_id,t.order_id,t.status,t.attempts,o.supplier_product_id,o.product_title,o.quantity,o.amount_usdt,o.catalog_version FROM faka_procurement_tasks t JOIN faka_orders o ON o.order_id=t.order_id WHERE t.status='queued' ORDER BY t.created_at LIMIT 20").all();return json({ok:true,count:results.length,items:results})}
 const resultMatch=path.match(/^\/v1\/internal\/procurement-jobs\/([a-f0-9-]{36})\/result$/i);
 if(resultMatch&&req.method==="POST"){if(!auth(req,env.PROCUREMENT_EXECUTOR_TOKEN))return json({ok:false,error:"unauthorized"},401);if(env.PROCUREMENT_ENABLED!=="true")return json({ok:false,error:"procurement_paused"},423);let b;try{b=await req.json()}catch{return json({ok:false,error:"invalid_json"},400)}const status=String(b.status||"");if(!["purchased","manual_review","failed"].includes(status))return json({ok:false,error:"invalid_result_status"},400);const task=await env.DB.prepare("SELECT * FROM faka_procurement_tasks WHERE order_id=?").bind(resultMatch[1]).first();if(!task)return json({ok:false,error:"task_not_found"},404);if(["purchased","manual_review"].includes(task.status))return json({ok:false,error:"task_terminal"},409);if(status==="purchased"&&(!String(b.supplier_order_ref||"").trim()||!String(b.delivery_ciphertext||"").trim()))return json({ok:false,error:"purchase_requires_supplier_ref_and_encrypted_delivery"},400);const at=now();await env.DB.batch([env.DB.prepare("UPDATE faka_procurement_tasks SET status=?,attempts=attempts+1,last_error=?,updated_at=? WHERE order_id=? AND status='queued'").bind(status,status==="failed"?"executor_reported_failure":null,at,resultMatch[1]),env.DB.prepare("UPDATE faka_orders SET procurement_status=?,delivery_status=?,supplier_order_ref=?,delivery_ciphertext=?,purchased_at=?,updated_at=?,error_code=? WHERE order_id=?").bind(status,status==="purchased"?"ready":"not_ready",b.supplier_order_ref||null,b.delivery_ciphertext||null,status==="purchased"?at:null,at,status==="manual_review"?"manual_review":status==="failed"?"procurement_failed":null,resultMatch[1])]);await audit(env,"procurement_result",resultMatch[1],{status});return json({ok:true,status})}
 return json({ok:false,error:"not_found"},404);
}};