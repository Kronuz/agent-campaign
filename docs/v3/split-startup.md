# Startup cost of the campaign_tool split

`campaign status` and `campaign brief` in the campaign-v3 home, 20 interleaved runs each, at
agent-campaign:b6929d5 (one 9,905-line `campaign_tool.py`) and at the split (the
`scripts/campaign_tool/` package, agent-campaign:a51b362). Job 261121-121457-598e48 under
`quiet strict` beside 5 light holds, Python 3.15.2, 2026-11-21.

| command | base median (ms) | split median (ms) | split / base | base min | split min |
|---|---:|---:|---:|---:|---:|
| campaign status | 1536.3 | 1522.2 | 0.991 | 1458.9 | 1388.5 |
| campaign brief | 1141.7 | 1124.9 | 0.985 | 1050.0 | 1047.5 |

Within noise: importing 24 cached modules costs no more than one large cached module.
