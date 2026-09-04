#!/usr/bin/env bash
# NOT executed by the harness.
curl --location 'http://localhost:8857/tix-flight-sabre-gds-integrator/issued' \
  --request POST \
  --header 'storeId: TIKETCOM' \
  --header 'channelId: WEB' \
  --header 'username: guest' \
  --header 'serviceId: GATEWAY' \
  --header 'requestId: 2a0335e1-7deb-4d2f-bec6-d763be54f255' \
  --header 'Content-Type: application/json' \
  --header 'Accept: application/json' \
  --data '{"distributionType": "sabre-gds", "account": {"code": "tiketcomSabre", "name": "tiketcomSabre"}, "bookingCode": "PSHPQY", "paxes": [{"paxNumber": 10000, "type": "adult", "title": "mr", "gender": "male", "firstName": "Manish", "lastName": "Aggarwal", "originalFirstName": "Manish", "originalLastName": "Aggarwal", "nationality": "IN", "dob": "1998-08-13", "passport": {"id": "A1234567", "expired": "2036-12-17", "country": "IN", "issuingDate": "2017-12-06"}}], "additionalData": {}}'
