# LSJ Ingestion Status

**Resource**: A Greek-English Lexicon (Liddell-Scott-Jones)
**Resource ID**: `LLS:46.30.25`

## Current Progress

- **Articles walked**: 144,677
- **Chunks checkpointed**: ~144,727
- **Last article**: `R.P.6761.S3` (Pi section)
- **Walk complete**: No (~65-70% done, ~9-11 hours remaining)
- **Chain breaks**: 12 (all `.COM` articles in abbreviations front-matter, all auto-recovered)

## How to Resume

From the `marginalia-plugin-logos` directory:

```bash
PYTHONPATH="/home/john/repos/MarginaliaAI/packages/core/src:$PYTHONPATH" \
RE_DB_URL="postgresql://re_dev:re_dev_pass@localhost:5435/research_engine" \
uv run python -c "
import asyncio
from logos.tools.ingest_book import _walk_and_checkpoint

async def main():
    result = await _walk_and_checkpoint('LLS:46.30.25')
    import json
    print(json.dumps(result, indent=2, default=str))

asyncio.run(main())
"
```

It will automatically resume from article `R.P.6761.S3` and continue the walk.

## Check Progress

```bash
psql "postgresql://re_dev:re_dev_pass@localhost:5435/research_engine" \
  -c "SELECT total_articles, last_article_id, walk_complete FROM logos_ingest_progress WHERE resource_id = 'LLS:46.30.25';"
```

## If You Need to Start Over

```bash
psql "postgresql://re_dev:re_dev_pass@localhost:5435/research_engine" \
  -c "DELETE FROM logos_ingest_chunks WHERE resource_id = 'LLS:46.30.25';" \
  -c "DELETE FROM logos_ingest_progress WHERE resource_id = 'LLS:46.30.25';"
```

## Notes

- The walk runs at ~110 articles/minute (~1.8/sec), limited by Logos API rate
- 12 abbreviation articles ending in `.COM` return 403 Forbidden — these are auto-skipped via chain-break recovery
- The actual lexicon entries (R.A.*, R.B.*, etc.) all load without issues
- Phase 2 (embedding + storing to corpus) has not been run yet — after the walk completes, re-run with an ingestion client to store to corpus
