# Security

Do not commit or share any of the following:

- `.browser-profile/` — persistent browser cookies and account state;
- `web_config.json` — the private activity URL captured during setup;
- `logs/` — diagnostic logs and failure screenshots;
- local virtual environments.

These paths are excluded by `.gitignore`. Before publishing a fork, verify with:

```powershell
git status --ignored
git grep -n -E "Authorization|Bearer|github_pat_|gho_"
```

If browser state has been exposed, sign out of Jielong, remove the exposed files
from Git history, and complete QR login again with a fresh `.browser-profile`.

Please report vulnerabilities privately through GitHub's security advisory
feature instead of opening a public issue containing credentials or activity URLs.
