# Profile maintenance

The README uses local SVG cards so visitors can see the last successful update
even when GitHub Actions or an API is temporarily unavailable.

| Workflow | Runs automatically | Updates |
| --- | --- | --- |
| Generate Contribution Snake | 00:43 and 12:43 UTC; changes to its workflow on `main` | Light and dark snake SVGs on `output` |
| Refresh Stat Cards | 06:17 UTC; changes to its workflow, stats script, or cards on `main` | `cadden-stats.svg` and `cadden-langs.svg` on `main` |

Both can also be started from **Actions → workflow name → Run workflow → main**.
GitHub may delay scheduled runs; public repositories can have schedules disabled
after 60 days without repository activity. See [GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Authentication

No manually created token is required. Both workflows use GitHub's automatic
`GITHUB_TOKEN`, with `contents: write` to publish their generated files.

`PROFILE_TOKEN` is an optional repository Actions secret for contribution data
visible to that token. It does not guarantee access to all private activity;
GitHub's profile visibility settings and the token's permissions still apply.

The previous workflows always preferred a configured `PROFILE_TOKEN`, even if
GitHub rejected it. That caused the observed `401 Unauthorized` / `Bad credentials`
failures. The snake now retries with the automatic token if that attempt fails;
the stats script also falls back when the optional token cannot authenticate.
You can remove an expired secret under **Settings → Secrets and variables → Actions**
to avoid repeated warnings, or replace it if you need its additional visibility.
Never put a token in the README, workflow YAML, or script.

## What the cards measure

- Contributions cover the trailing year. Token visibility, GitHub's update delays,
  and differing refresh times can make the card and snake disagree temporarily.
- Repository count is the account's public repositories, including forks.
- Language totals and percentages use language bytes from owned public repositories
  excluding forks. They describe the code in those repositories, not proficiency
  or time spent coding.
- Pull requests count authored public pull requests across GitHub.

The stats workflow commits only changed cards and retries ordinary pushes if `main`
moves during a run. The snake preserves `output` history and skips empty commits.
If branch rules reject a write, the Actions log will identify the blocked push;
the existing images remain available.
