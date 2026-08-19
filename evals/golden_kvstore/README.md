# KVStore

Embedded persistent key-value store with TTL support.

## Installation
```bash
pip install -e .
```

## CLI Usage
```bash
kvstore set api_key "secret-123" --ttl 3600
kvstore get api_key
kvstore ls
kvstore delete api_key
kvstore stats
```
