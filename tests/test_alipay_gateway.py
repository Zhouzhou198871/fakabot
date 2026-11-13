import base64
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from alipay_gateway import build_page_pay_url


class AlipayGatewayTests(unittest.TestCase):
    def test_signed_sandbox_page_pay_url(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'app.pem'
            path.write_bytes(private.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            url = build_page_pay_url('ALTEST001', '16.00', 'test item', app_id='sandbox-app',
                private_key_path=str(path), notify_url='https://notify.example/notify')
        parsed = urlparse(url)
        self.assertEqual(parsed.netloc, 'openapi.alipaydev.com')
        params = {k:v[0] for k,v in parse_qs(parsed.query).items()}
        signature = base64.b64decode(params.pop('sign'))
        message = '&'.join(f'{k}={params[k]}' for k in sorted(params))
        private.public_key().verify(signature, message.encode(), padding.PKCS1v15(), hashes.SHA256())
        self.assertEqual(params['app_id'], 'sandbox-app')
        self.assertEqual(params['method'], 'alipay.trade.page.pay')
        self.assertIn('ALTEST001', params['biz_content'])
        self.assertEqual(params['notify_url'], 'https://notify.example/notify')

    def test_requires_app_id_and_https_notify_url(self):
        with self.assertRaisesRegex(RuntimeError, 'ALIPAY_APP_ID'):
            build_page_pay_url('ALTEST001', '1.00', 'item', app_id='', private_key_path='/missing')


if __name__ == '__main__':
    unittest.main()