# Config samples

Each plugin reads `~/.taku/<tool>.json` (override the dir with `TAKU_DIR`;
legacy `~/.<tool>/config.json` is still honored). Copy the matching example
out of this directory, drop it into `~/.taku/`, and fill in real values:

```bash
mkdir -p ~/.taku
cp examples/zeppelin.example.json ~/.taku/zeppelin.json
chmod 600 ~/.taku/zeppelin.json     # contains a password / session token
```

| Plugin | Single-environment | Multiple environments |
| --- | --- | --- |
| zeppelin  | [`zeppelin.example.json`](./zeppelin.example.json)   | [`zeppelin-profiles.example.json`](./zeppelin-profiles.example.json) |
| starrocks | [`starrocks.example.json`](./starrocks.example.json) | [`starrocks-profiles.example.json`](./starrocks-profiles.example.json) |
| mysql     | [`mysql.example.json`](./mysql.example.json)         | same `profiles` shape as zeppelin/starrocks |
| mongodb   | [`mongodb.example.json`](./mongodb.example.json)     | same `profiles` shape as zeppelin/starrocks |
| alidocs   | [`alidocs.example.json`](./alidocs.example.json)     | same `profiles` shape (selector: `ALIDOCS_PROFILE`) |

## Multiple environments (profiles)

All five plugins share the same multi-environment shape. Replace a flat config
with a `profiles` map and pick one at runtime with `--profile NAME` (or the
plugin's `<TOOL>_PROFILE` env var):

```json
{
  "default_profile": "prod",
  "cache_ttl_days": 30,
  "profiles": {
    "prod":    { "...": "..." },
    "staging": { "...": "..." }
  }
}
```

Keys at the top level (outside `profiles`) act as defaults merged into every
profile. A flat config with no `profiles` key is the implicit "default" profile
and keeps its cache un-namespaced — handy for single-env setups.

Selection order: `--profile` CLI flag → `<TOOL>_PROFILE` env var →
`default_profile` config key → the sole profile if there's only one.

JSON files don't allow comments — keep secrets in the file itself, not inline
docs, and rely on `chmod 600`.
