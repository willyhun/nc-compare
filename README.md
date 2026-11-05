# nc-compare / nc-fix

- **`nc-compare.py`** compares the files stored in a Nextcloud account with a local copy, **read-only**.
- **`nc-fix.py`** optionally overwrites the files it flagged with their verified local copy (dry run by default).

For every file it checks:

1. **Recorded size:** the size Nextcloud reports over WebDAV (`getcontentlength`, which comes from
   the `oc_filecache` table) against the real size of the local copy.
2. **Content:** it downloads the file and compares its MD5 with the local copy. Use `--size-only`
   to skip this step.

It only sends `PROPFIND` and `GET` requests and never changes anything on the server or locally.

## Why this exists

After a series of upgrades, a Nextcloud instance with server-side encryption had files whose
**recorded size was wrong**. The files were old, written in the pre-binary (base64) encryption
format, and the database still held the base64-inflated size. Such a file decrypts fine and passes
`occ files:scan`, `occ maintenance:repair` and the encryption checks. But the server announces the
wrong `Content-Length` and then sends the real, shorter content, so:

- the desktop client fails with **"An unexpected error occurred"**, or with
  **"The start offset (N) exceeded the size of the entity (M)"** when it resumes;
- `curl` over HTTP/2 ends with **exit 92** (stream reset), even though the full content arrived;
- the **Nextcloud log shows nothing**, because no exception is thrown.

`SIZE_WRONG` in this tool's output is exactly that case: identical content, wrong recorded size.

## Requirements

- `python3` (3.8+, standard library only)
- `curl` 7.63 or newer (HTTP/2 support recommended)
- the local copy, for example the desktop client's sync folder, with the client **paused** while
  comparing

## Account and app password

The tool reads files through WebDAV as one Nextcloud account and compares that account's files.
Don't use the account's login password. Create an **app password** you can revoke afterwards.

**As the user, in the web interface:**
Settings (avatar menu) → **Personal settings** → **Security** → *Devices & sessions* → enter a name
such as `nc-compare` → **Create new app password**. Copy the password; it is shown only once.
Delete it in the same list when you are done.

**As an administrator, on the server** (if your version has the command, check with
`occ list user`):

```bash
occ user:add-app-password <user>
```

**A separate account instead of personal credentials.** To compare without using a person's
credentials, create a dedicated account and share the folders to it **read-only**:

```bash
occ user:add --display-name="nc-compare" nc-compare
# then share the folder(s) to "nc-compare" read-only (web UI: Share → user nc-compare,
# untick "Allow editing"), and create an app password for nc-compare as above
```

Paths are then as that account sees them (shared folders appear under the share name), so point
`LOCAL` at a copy with the same layout, or use `--dir` and `--list` accordingly. This tool never
writes, so read-only sharing is for your peace of mind.

The password reaches curl through a temporary config file (mode 600) in the output folder, which is
deleted at the end. It never appears on a command line or in the process list.

## Usage

```bash
cat > nc-env.local.sh <<'X'       # your values; never share this file
export NC=https://cloud.example.com
export U=alice
export LOCAL="$HOME/Nextcloud"
# export P='app-password'         # or leave it out and be prompted
X
chmod 600 nc-env.local.sh
. ./nc-env.sh

./nc-compare.py                          # whole account: server tree vs LOCAL tree
./nc-compare.py --dir Documents          # one folder
./nc-compare.py --list files.txt         # only the listed paths (relative to the files root)
./nc-compare.py --size-only              # recorded size only, no downloads (fast first pass)
```

Options: `-j N` parallel downloads (default 2), `--out DIR` output folder,
`--h1-timeout S` limit for the HTTP/1.1 fallback (default 300), `--exclude PATTERN` extra base-name
pattern to skip (repeatable), `--no-default-excludes` to include the sync client's own files.

A sensible order: run `--size-only` first (minutes, no downloads); then a full content run, or a
content run on just the suspicious files:

```bash
./nc-compare.py --size-only --out /var/tmp/ncc-size
awk -F'\t' '$1=="SIZE_DIFF"{print $10}' /var/tmp/ncc-size/results.tsv > /var/tmp/ncc-suspects.txt
./nc-compare.py --list /var/tmp/ncc-suspects.txt --out /var/tmp/ncc-suspects
```

## Output

`results.tsv` in the output folder (default `/var/tmp/nc-compare-<timestamp>`, mode 700, because
file names can be sensitive):

| column | meaning |
|---|---|
| status | see below |
| transport | `h2`, `http/1.1`, `h2-incomplete-ok` (stream error at the end, but the full content arrived), `h1-fallback` |
| curl_exit / http | curl exit code and HTTP status of the download used |
| reported_size | size Nextcloud reports |
| local_size | size of the local copy |
| received | bytes actually downloaded |
| local_md5 / remote_md5 | content hashes |
| path | path relative to the files root |

| status | meaning |
|---|---|
| `OK` | size and content match |
| `SIZE_WRONG` | content identical, recorded size wrong: clients will fail on this file |
| `CONTENT_DIFF` | content differs |
| `DL_FAIL` | download failed (HTTP error) or stayed incomplete |
| `SIZE_DIFF` | (`--size-only`) recorded size differs from the local size |
| `MISSING_LOCAL` | on the server, not in `LOCAL` |
| `MISSING_REMOTE` | in `LOCAL` (or in the list), not on the server |

Exit code: 0 if everything is `OK`, 1 if anything differs, 2 for setup errors.

Sync client files (`.sync_*.db`, `.~lock.*`, `~$*`, `desktop.ini`, `.DS_Store`, …) are skipped on
both sides by default.

## HTTP/2 and the HTTP/1.1 fallback

Downloads use HTTP/2 when the server offers it. When the recorded size is wrong, the HTTP/2 stream
is reset at the end (curl exit 92). If the MD5 of what arrived still matches the local copy, the file
counts as received (`h2-incomplete-ok`). That is safe because a truncated download cannot match. If
it doesn't match, the download is retried once over HTTP/1.1 with `--ignore-content-length`.
This is slow: curl only stops when the server closes the idle connection, which can take a minute
or more per file. `--h1-timeout` caps it.

## Fixing: `nc-fix.py`

`nc-fix.py` reads the `results.tsv` from `nc-compare.py` and overwrites the flagged files on the
server with their local copy: a WebDAV `PUT` to the same path, so the **file ID stays**, along with
its shares, tags, comments and favorites. Nextcloud then records the right size and, with
server-side encryption, re-encrypts the file in the current format.

- **`SIZE_WRONG`** (default): server and local content are proven identical. Only the recorded size
  changes. This is the safe case.
- **`CONTENT_DIFF`**: only with `--include-content-diff`. The **local copy replaces different
  content** on the server. Use it only when you are sure the local copy is the right one.
- Other statuses (`MISSING_*`, `DL_FAIL`) are never touched.

```bash
./nc-fix.py /var/tmp/nc-compare-…/results.tsv                     # dry run: what would be fixed
./nc-fix.py /var/tmp/nc-compare-…/results.tsv --apply             # asks for confirmation
./nc-fix.py … --apply --include-content-diff --backup /var/tmp/ncfix-backup
```

Before each upload it checks again that the **local copy and the server copy still have the MD5
`nc-compare.py` recorded**. A file changed by anyone in the meantime is skipped
(`SKIP_LOCAL_CHANGED` / `SKIP_REMOTE_CHANGED`). After the upload it checks the **file ID**, the
**reported size**, and the **MD5 of a fresh download**.

| status | meaning |
|---|---|
| `WOULD_FIX` | dry run: all checks passed, would be uploaded |
| `FIXED` | uploaded and verified |
| `SKIP_LOCAL_CHANGED` / `SKIP_REMOTE_CHANGED` / `SKIP_LOCAL_MISSING` | not touched: run `nc-compare.py` again |
| `PUT_FAIL` | upload refused (HTTP status in the `http` column) |
| `ID_CHANGED` | uploaded, but the file got a new ID (shares etc. may be detached) |
| `SIZE_STILL_WRONG` | uploaded, reported size still differs |
| `VERIFY_FAIL` | uploaded, fresh download doesn't match the local copy |

Results go to `fix-results.tsv` in `--out` (default `/var/tmp/nc-fix-<timestamp>`). Run
`nc-compare.py` again afterwards: the fixed files should be `OK`.

### What happens to the previous content

Nextcloud keeps the overwritten content as a **version** only if the **Versions app** is enabled.
`nc-fix.py` asks the server and prints the answer before a real run. **Versions are not a backup:**

- they expire by age;
- they are removed automatically when they take up too much of the user's quota;
- they disappear when the file is deleted;
- an admin can remove them (`occ versions:cleanup`).

If a choice turns out wrong, restore the previous version in the web UI (file → *Versions*) while it
still exists. For anything you can't recreate, use **`--backup DIR`**: each server copy is saved
there **unencrypted** before it is overwritten (folder mode 700). Delete it when you're done.

`nc-fix.py` refuses `--include-content-diff` when versions are not enabled and no `--backup` is
given, unless you add `--yes`.

Side effects to expect:

- one new version per fixed file (space in the user's storage until versions expire or are
  cleaned up);
- the desktop clients download each fixed file once more (same content, new ETag);
- with server-side encryption the file is re-encrypted.

## License

Copyright (C) 2026 The nc-compare authors.

GNU General Public License v3.0 or later (`GPL-3.0-or-later`); see [LICENSE](LICENSE).
This program comes with ABSOLUTELY NO WARRANTY. It is free software, and you are welcome to
redistribute it under the conditions of that license.
