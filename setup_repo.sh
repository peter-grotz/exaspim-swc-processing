#!/bin/sh
# Creates a GitHub repo for exaspim-swc-processing and pushes the initial commit.
# Edit VISIBILITY below if needed, then run: sh setup_repo.sh
set -eu

REPO="peter-grotz/exaspim-swc-processing"
DESCRIPTION="Processing and packaging of exaSPIM neuron SWC reconstructions into per-cell AIND derived data assets."
VISIBILITY="private"
SCRIPT_NAME=$(basename "$0")

uv sync
gh auth status >/dev/null 2>&1 || gh auth login

git init --initial-branch dev
git add .
git commit -m "feat: initial commit"

gh repo create "$REPO" --"$VISIBILITY" --description "$DESCRIPTION" --source=. --push

git checkout -b main
git push origin main
git checkout dev

echo "Done. Repo created at https://github.com/$REPO"
echo "Please setup branch protection rules for main and dev branches, and add collaborators as needed."

rm -f "$SCRIPT_NAME"
