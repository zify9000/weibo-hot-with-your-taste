# Weibo Hot Band API

## Required Headers

`https://weibo.com/ajax/statuses/hot_band` requires a `Referer` header or returns **0 results**.

### Working headers
```python
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://weibo.com",
}
```

### Broken (no Referer)
```python
# Returns: {"data": {"band_list": []}}
r = requests.get("https://weibo.com/ajax/statuses/hot_band", timeout=10)
```

## Response structure
```json
{
  "data": {
    "band_list": [
      {
        "word": "话题名称",
        "word_scheme": "话题标签",
        "category": "分类",
        "field_tag": "领域标签",
        "raw_hot": 1234567,
        "hot_str": "123.4万",
        "realpos": 1
      }
    ]
  }
}
```

- `realpos == 0`: non-ranking items (e.g. "更多热搜"), skip these
- `hot_str`: pre-formatted hotness string
- `raw_hot`: raw numeric hotness value
