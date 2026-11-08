# Publishing this site on GitLab Pages

The documentation you are reading is built with [MkDocs Material](https://squidfunnel.github.io/mkdocs-material/) from the `docs/` folder of the
[GitHub repository](https://github.com/sadiqgpasha89/FinRobot), and published by the `.gitlab-ci.yml` at the root of that repository. The Mermaid diagrams
render in the browser, so no diagram tooling is needed in the pipeline.

## Option A: the GitLab project holds the repository

1. In GitLab choose **New project → Import project → GitHub** and select `sadiqgpasha89/FinRobot` (or add a GitLab remote and push `main`).
2. The `.gitlab-ci.yml` is already in the repository. On the default branch the `pages` job builds the site and publishes it; on other branches and merge
   requests `docs:check` builds it without publishing, and `--strict` fails the build on a broken link.
3. Open **Deploy → Pages** for the address, normally `https://<namespace>.gitlab.io/<project>/`. Under **Deploy → Pages** you can set who may view it.

A GitHub import is a copy at that moment. To keep it current, push from GitHub to GitLab (a GitHub Actions workflow or a second remote), or use GitLab's pull
mirroring where your plan includes it.

## Option B: the GitLab project holds only the pipeline

Use this when the GitHub repository stays the single source of truth.

1. Create an empty GitLab project and commit one file to it: a copy of [`.gitlab-ci.yml`](https://github.com/sadiqgpasha89/FinRobot/blob/main/.gitlab-ci.yml).
2. Under **Settings → CI/CD → Variables** you may set `GITHUB_REPO_URL` and `GITHUB_REF` (the defaults are this repository's `main`). A private repository needs a
   masked token in the URL, for example `https://<user>:<token>@github.com/<owner>/<repo>.git`.
3. Because there is no `mkdocs.yml` in that project, each build clones the GitHub repository and builds it.
4. Add a schedule under **Build → Pipeline schedules** (weekly or daily) so the site follows the GitHub repository; run it once by hand first.

!!! note "Branch must contain the site"
    The clone must contain `mkdocs.yml`, `requirements-docs.txt` and `docs/`. Until the branch that adds them is merged into `main`, set `GITHUB_REF` to that branch.

## Build and preview locally

```bash
pip install -r requirements-docs.txt
mkdocs serve                      # http://127.0.0.1:8000, reloads on save
mkdocs build --strict             # the same check the pipeline runs; writes ./site
```

## What is in the pipeline

| Job | When | What |
|---|---|---|
| `docs:check` | branches other than the default, merge requests | `mkdocs build --strict`, nothing published |
| `pages` | default branch, schedules, manual or API triggers | the same build into `public/`, published as the Pages site |

Versions are pinned in `requirements-docs.txt`. Material for MkDocs has announced end of life on 5 May 2027 with its successor, Zensical, reading the same
`mkdocs.yml`; plan a migration before then.
