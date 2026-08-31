# Multi-city integrator run dumps

The **primary dump** is the Confluence child page (fare summary, curls, request JSON, small responses). Search responses are ~1 MB and do not fit on Confluence (Docker also cannot attach host files), so they live here:

`runs/<TC-id>/<timestamp>/01_search.response.json.gz.b64`

That file is compact JSON, gzipped, then base64. Decode:

```bash
python3 -c "import base64,gzip,sys; sys.stdout.buffer.write(gzip.decompress(base64.b64decode(sys.stdin.read())))" < 01_search.response.json.gz.b64 > 01_search.response.json
```

The Google Sheet **Dump** column links the Confluence page, not these files.

Written by GitHub MCP as `manish-aggarwal-89`.
