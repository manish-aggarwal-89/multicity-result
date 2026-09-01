# Multi-city integrator run dumps

Overflow store for the Sabre-GDS Multi-City integrator MCP. The **primary dump is the Confluence
child page** under [Sabre-GDS Multi-City run dumps](https://borobudur.atlassian.net/wiki/spaces/~71202004c18360a29f474988bb0ada2ca9eae4/pages/4924145709):
verdict, fare summary, every curl, every request body, and the small response bodies.

Response bodies too big to sit on a wiki page land here, byte-identical to the run:

```
runs/<TC-id>/<timestamp>/<NN>_<step>.response.json
runs/<TC-id>/<timestamp>/01_search.response.json.gz.b64
```

`.response.json` is indented JSON, so GitHub renders and diffs it. The search body is ~1 MB, which
the contents API will not take in one write, so it is compact JSON, gzipped, then base64:

```bash
curl -sL https://raw.githubusercontent.com/manish-aggarwal-89/multicity-result/main/runs/TC-011/2026-08-31_18-38-52_b211e5/01_search.response.json.gz.b64 \
  | python3 -c "import base64,gzip,sys; sys.stdout.buffer.write(gzip.decompress(base64.b64decode(sys.stdin.read())))" > 01_search.response.json
```

The results Google Sheet **Dump** column links the Confluence page, never a file in here.
