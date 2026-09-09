"""Private stdio worker for model calls. No storage or runtime configuration IO."""

from __future__ import annotations

import asyncio
import json
import sys


def main() -> None:
    # Keep SDK prints (including import-time diagnostics) off the protocol pipe.
    replies = sys.stdout
    sys.stdout = sys.stderr
    with asyncio.Runner() as runner:
        for line in sys.stdin:
            try:
                kwargs = json.loads(line)
                from litellm import acompletion

                response = runner.run(acompletion(**kwargs))
                result = (
                    response if isinstance(response, dict) else response.model_dump(mode="json")
                )
                payload = {"result": result}
            except Exception as exc:
                # Provider exceptions may embed request bodies or credentials.
                # Return the failure type, never repr(request) or exception text.
                payload = {"error": type(exc).__name__}
            replies.write(json.dumps(payload, ensure_ascii=False) + "\n")
            replies.flush()


if __name__ == "__main__":
    main()
