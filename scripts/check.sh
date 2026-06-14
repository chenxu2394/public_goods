#!/usr/bin/env bash
set -euo pipefail

uv run pytest
uv run djlint templates
