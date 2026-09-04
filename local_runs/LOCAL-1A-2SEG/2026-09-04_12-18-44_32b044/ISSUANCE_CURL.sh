#!/usr/bin/env bash
# NOT executed by the harness.
curl --location 'http://localhost:8857/tix-flight-sabre-gds-integrator/issued' \
  --request POST \
  --header 'storeId: TIKETCOM' \
  --header 'channelId: WEB' \
  --header 'username: guest' \
  --header 'serviceId: GATEWAY' \
  --header 'requestId: a8440885-09fa-4fdd-93d1-aa6db1c73fa1' \
  --header 'Content-Type: application/json' \
  --header 'Accept: application/json' \
  --data '{"distributionType": "sabre", "account": {"code": "tiketcomSabre", "name": "tiketcomSabre"}, "bookingCode": "PSBYQS", "paxes": [{"paxNumber": 10000, "type": "adult", "title": "mr", "gender": "male", "firstName": "Manish", "lastName": "Aggarwal", "originalFirstName": "Manish", "originalLastName": "Aggarwal", "nationality": "IN", "dob": "1998-08-13", "passport": {"id": "A1234567", "expired": "2036-12-17", "country": "IN", "issuingDate": "2017-12-06"}}], "additionalData": {}}'
