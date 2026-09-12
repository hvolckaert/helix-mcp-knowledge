# Optional component dependency locks

OCR, semantic search, and result reranking are downloaded only after an administrator
enables them in the local dashboard. Their independent virtual environments are installed
from the checksum-locked files packaged under
`src/helix_mcp_knowledge/resources/requirements`.

Regenerate the locks from the repository root with the pinned CI tool:

```bash
uvx --from uv==0.11.16 uv pip compile \
  --universal --generate-hashes \
  requirements/pip-bootstrap.in \
  --output-file src/helix_mcp_knowledge/resources/requirements/pip-bootstrap.txt

uvx --from uv==0.11.16 uv pip compile \
  --universal --generate-hashes \
  requirements/ocr-component.in \
  --output-file src/helix_mcp_knowledge/resources/requirements/ocr-component.txt

uvx --from uv==0.11.16 uv pip compile \
  --upgrade --universal --generate-hashes --emit-index-url --torch-backend cpu \
  requirements/semantic-component.in \
  --output-file src/helix_mcp_knowledge/resources/requirements/semantic-component.txt

uvx --from uv==0.11.16 uv pip compile \
  --upgrade --universal --generate-hashes --emit-index-url --torch-backend cpu \
  requirements/reranker-component.in \
  --output-file src/helix_mcp_knowledge/resources/requirements/reranker-component.txt
```

The semantic and reranker locks use uv's explicit `--torch-backend cpu` resolution.
Both installers supply the restricted official
`https://download.pytorch.org/whl/cpu/torch/` artifact listing at install time so
`pip` can resolve the `+cpu` wheel without exposing unrelated packages to a secondary
index. Every package remains exactly pinned and every accepted artifact is verified by
SHA-256. Preserve and verify the resulting Torch hashes after regeneration, then run
every dependency audit and all optional-component CI suites before publishing.
