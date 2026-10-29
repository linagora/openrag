# Linting Setup Guide

This project uses **Ruff** for Python linting and code formatting. All developers must ensure their code passes linting checks before submitting pull requests.

## Installation

The linting dependencies are already included in `pyproject.toml`. Install them with:

```bash
uv sync --group lint
```

Or install all groups including dev:

```bash
uv sync
```

## Running Linting Locally

### Check for linting issues

```bash
uv run ruff check .
```

### Auto-fix linting issues

```bash
uv run ruff check . --fix
```

### Check code formatting

```bash
uv run ruff format --check .
```

### Auto-format code

```bash
uv run ruff format .
```

## Ruff Configuration

Ruff configuration is defined in `pyproject.toml` under `[tool.ruff]` and `[tool.ruff.lint]` sections.

### Key Rules Enabled

- **E, W**: PEP 8 errors and warnings
- **F**: Pyflakes (undefined names, unused imports)
- **I**: isort-style import sorting
- **C4**: flake8-comprehensions (optimization suggestions)
- **UP**: pyupgrade (modernize Python code)
- **PIE**: flake8-pie (misc linting rules)
- **RUF**: Ruff-specific rules (**`not applied currently`**)

### Excluded Directories

The following directories are excluded from linting:
- `.git`, `.venv`, `.pytest_cache`, `.ruff_cache`, `.vscode`
- `node_modules`, `vdb`, `ray_mount`, `model_weights`, `logs`
- `.astro`, `openrag.egg-info`

## CI/CD Integration

The `lint` job of the CI workflow (`.github/workflows/ci.yml`) runs on every push to main and develop, and on every pull request that changes more than the UI or the docs. It:

1. Checks for linting issues with `ruff check`
2. Verifies code formatting with `ruff format --check`
3. Reports results directly to GitHub (with file and line annotations)

### Pull Request Requirements

All pull requests must pass the linting CI check. If your PR fails:

1. Run `uv run ruff check . --fix` to auto-fix issues
2. Run `uv run ruff format .` to format the code
3. Review any remaining issues that require manual fixes
4. Commit and push your changes

## Pre-commit Hook (Optional)
One can also setup a pre-commit hook to run linting automatically before each commit.

```bash
cat > .git/hooks/pre-commit << 'EOF'
#!/bin/bash

# Get list of staged Python files
FILES=$(git diff --cached --name-only --diff-filter=ACM | grep '\.py$')

if [ -z "$FILES" ]; then
  exit 0
fi

# Run ruff check (will auto-fix if possible)
echo "Running ruff check..."
uv run ruff check $FILES --fix

# Run ruff format
echo "Running ruff format..."
uv run ruff format $FILES

# Re-add the modified files
git add $FILES

# Run ruff check again without --fix to catch remaining errors
echo "Checking for remaining issues..."
uv run ruff check $FILES

# If ruff check fails, block the commit
if [ $? -ne 0 ]; then
  echo "Ruff check failed. Please fix errors before committing."
  exit 1
fi

exit 0
EOF

chmod +x .git/hooks/pre-commit
```

* To disable pre-commit

```bash
rm .git/hooks/pre-commit
```

## Troubleshooting

### "ruff: command not found"
Make sure you've installed the lint dependencies:
```bash
uv sync --group lint
```

### Auto-fix doesn't resolve all issues
Some issues require manual fixes. Review the error messages and fix them manually, then commit again.
Sometimes, you will have to ignore a particular check if deemed unnecessary

```python
# Example: C417 rule suggest that map here is unnecessary 


docs_with_tokens = list(map(lambda d: (_length_function(d.page_content), d), docs))  # noqa: C417

```