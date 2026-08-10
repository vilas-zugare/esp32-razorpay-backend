import requests
import json
import base64

key_id = 'rzp_live_TNKlkEBHhgdx3r'
key_secret = 'cQvkGP50yuNTtmCWZ2jJsVEH'
auth_str = f'{key_id}:{key_secret}'
b64_auth = base64.b64encode(auth_str.encode('ascii')).decode('ascii')
headers = {'Authorization': f'Basic {b64_auth}'}

# Check the last QR code the user generated
qr_id = 'qr_TNZVX1cuaJfPv4'
resp = requests.get(f'https://api.razorpay.com/v1/payments/qr_codes/{qr_id}', headers=headers)
print(json.dumps(resp.json(), indent=2))
