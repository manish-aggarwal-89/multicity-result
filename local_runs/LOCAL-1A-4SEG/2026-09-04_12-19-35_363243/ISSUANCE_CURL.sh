#!/usr/bin/env bash
# NOT executed by the harness.
curl --location 'http://localhost:8857/tix-flight-sabre-gds-integrator/issued' \
  --request POST \
  --header 'storeId: TIKETCOM' \
  --header 'channelId: WEB' \
  --header 'username: guest' \
  --header 'serviceId: GATEWAY' \
  --header 'requestId: e0fc214b-4184-4a68-9759-8f339188091a' \
  --header 'Content-Type: application/json' \
  --header 'Accept: application/json' \
  --data '{"distributionType": "sabre", "account": {"code": "tiketcomSabre", "name": "tiketcomSabre"}, "bookingCode": "PYQWYB", "paxes": [{"paxNumber": 10000, "type": "adult", "title": "mr", "gender": "male", "firstName": "Manish", "lastName": "Aggarwal", "originalFirstName": "Manish", "originalLastName": "Aggarwal", "nationality": "IN", "dob": "1998-08-13", "passport": {"id": "A1234567", "expired": "2036-12-17", "country": "IN", "issuingDate": "2017-12-06"}}], "additionalData": {}}'
