#!/usr/bin/env bash
# NOT executed by the harness.
curl --location 'http://localhost:8857/tix-flight-sabre-gds-integrator/issued' \
  --request POST \
  --header 'storeId: TIKETCOM' \
  --header 'channelId: WEB' \
  --header 'username: guest' \
  --header 'serviceId: GATEWAY' \
  --header 'requestId: 180e7d7f-6909-4343-99e5-ecd769ec6405' \
  --header 'Content-Type: application/json' \
  --header 'Accept: application/json' \
  --data '{"distributionType": "sabre-gds", "account": {"code": "tiketcomSabre", "name": "tiketcomSabre"}, "bookingCode": "PGYZSJ", "paxes": [{"paxNumber": 10000, "type": "adult", "title": "mr", "gender": "male", "firstName": "Manish", "lastName": "Aggarwal", "originalFirstName": "Manish", "originalLastName": "Aggarwal", "nationality": "IN", "dob": "1998-08-13", "passport": {"id": "A1234567", "expired": "2036-12-17", "country": "IN", "issuingDate": "2017-12-06"}}], "additionalData": {}}'
