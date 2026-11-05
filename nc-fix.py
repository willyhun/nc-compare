#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The nc-compare authors
#
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version. It is distributed WITHOUT ANY WARRANTY; see the
# LICENSE file or <https://www.gnu.org/licenses/>.
"""
nc-fix.py - overwrite files in Nextcloud with their local copy, based on a
results.tsv from nc-compare.py. Dry run unless --apply is given.

What it fixes:
  SIZE_WRONG    (default) server and local content are identical, only the
                recorded size is wrong. Uploading the same bytes again makes
                Nextcloud record the right size (and, with server-side
                encryption, re-encrypts the file in the current format).
  CONTENT_DIFF  only with --include-content-diff: the LOCAL copy replaces
                DIFFERENT content on the server. Use only when you are sure
                the local copy is the right one.
Other statuses are ignored (MISSING_*, DL_FAIL: look at those yourself).

Per file:
  1. the local copy must still have the MD5 nc-compare saw     else SKIP_LOCAL_CHANGED
  2. the server copy must still have the MD5 nc-compare saw    else SKIP_REMOTE_CHANGED
     (with --backup DIR the server copy is saved there first)
  3. in-place WebDAV PUT, local mtime kept                     else PUT_FAIL
  4. same file ID as before (shares, tags, comments stay)      else ID_CHANGED
  5. reported size equals the local size                       else SIZE_STILL_WRONG
  6. fresh download has the local MD5                          else VERIFY_FAIL
  7. FIXED   (dry run stops after 2: WOULD_FIX)

About the previous content: Nextcloud keeps it as a version only if the
Versions app is enabled (checked and shown before a real run). Versions are
not a backup: they expire by age, are removed automatically when they take too
much of the user's quota, disappear when the file is deleted, and admins can
remove them (occ versions:cleanup). For anything you can't recreate, use
--backup DIR (plaintext copies of the server files, mode 700) or your own backup.

Settings: same environment as nc-compare.py (NC, U, P, LOCAL; see nc-env.sh).
LOCAL must be the same folder nc-compare.py used.
Output: <out>/fix-results.tsv (default /var/tmp/nc-fix-<timestamp>)
Exit code: 0 everything FIXED / WOULD_FIX, 1 anything else, 2 setup error.
"""
import argparse, csv, importlib.util, json, os, subprocess, sys, time

_spec = importlib.util.spec_from_file_location(
    'nccompare', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nc-compare.py'))
ncc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ncc)
die, local_md5 = ncc.die, ncc.local_md5


class FixDav(ncc.Dav):
    def fileid(self, rel):
        body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
                '<d:prop><oc:fileid/></d:prop></d:propfind>')
        p = subprocess.run(self._curl() + ['-X', 'PROPFIND', '-H', 'Depth: 0', '-H', 'Content-Type: application/xml',
                                           '--data', body, '--max-time', '120', self.url(rel)], capture_output=True)
        out = p.stdout.decode(errors='replace')
        i = out.find('fileid>')
        if i < 0:
            return ''
        return ''.join(ch for ch in out[i + 7:i + 40].split('<')[0] if ch.isdigit())

    def size(self, rel):
        info = self.propfind(rel, 0)
        return None if not info else next(iter(info.values()), None)

    def download(self, rel, expect_md5, save_to=None):
        """Lenient download like nc-compare: HTTP/2, accept a stream error at the end if the MD5
        matches, otherwise one HTTP/1.1 retry. Returns (ok, md5)."""
        def once(extra):
            cmd = self._curl() + ['-f'] + extra + ['-w', '%{stderr}\n%{http_code}\n', self.url(rel)]
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            import hashlib
            h = hashlib.md5()
            f = open(save_to, 'wb') if save_to else None
            try:
                for chunk in iter(lambda: p.stdout.read(1 << 20), b''):
                    h.update(chunk)
                    if f:
                        f.write(chunk)
            finally:
                if f:
                    f.close()
            p.stderr.read()
            return p.wait(), h.hexdigest()
        rc, md5 = once([])
        if rc not in (0, 22) and md5 != expect_md5:
            rc, md5 = once(['--http1.1', '--ignore-content-length', '--max-time', str(self.h1_timeout)])
        ok = md5 == expect_md5 and rc in (0, 18, 28, 92)
        if not ok and save_to:
            try:
                os.unlink(save_to)
            except OSError:
                pass
        return ok, md5

    def strict_md5(self, rel):
        rc, code, ver, n, md5 = self.get(rel)
        return md5 if rc == 0 else None

    def put(self, rel, local_path):
        mtime = str(int(os.path.getmtime(local_path)))
        p = subprocess.run(self._curl() + ['-o', '/dev/null', '-w', '%{http_code}', '-H', f'X-OC-Mtime: {mtime}',
                                           '-T', local_path, self.url(rel)], capture_output=True)
        return p.stdout.decode().strip() or '000'

    def versioning(self):
        p = subprocess.run(self._curl() + ['-H', 'OCS-APIRequest: true', '--max-time', '60',
                                           self.origin + self.root.split('/remote.php/')[0]
                                           + '/ocs/v1.php/cloud/capabilities?format=json'], capture_output=True)
        try:
            caps = json.loads(p.stdout)['ocs']['data']['capabilities']
            return bool(caps.get('files', {}).get('versioning'))
        except Exception:
            return None


def main():
    ap = argparse.ArgumentParser(description='Overwrite Nextcloud files with their verified local copy.',
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('results', help='results.tsv written by nc-compare.py')
    ap.add_argument('--apply', action='store_true', help='really upload (default: dry run)')
    ap.add_argument('--include-content-diff', action='store_true',
                    help='also overwrite CONTENT_DIFF files with the local copy')
    ap.add_argument('--backup', help='save each server copy here before overwriting it (plaintext!)')
    ap.add_argument('--yes', action='store_true', help='no confirmation prompt')
    ap.add_argument('--out', help='output folder (default: /var/tmp/nc-fix-<timestamp>)')
    ap.add_argument('--h1-timeout', type=int, default=300, help='max seconds for an HTTP/1.1 fallback download')
    a = ap.parse_args()

    base, user, local = os.environ.get('NC', ''), os.environ.get('U', ''), os.environ.get('LOCAL', '')
    if not base or 'example.com' in base:
        die('set NC to the Nextcloud base URL (see nc-env.sh)')
    if not user:
        die('set U to the Nextcloud login name')
    if not local or not os.path.isdir(local):
        die(f'set LOCAL to the local folder nc-compare.py used (now: {local!r})')

    wanted = {'SIZE_WRONG'} | ({'CONTENT_DIFF'} if a.include_content_diff else set())
    rows = []
    with open(a.results, encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f, delimiter='\t', quoting=csv.QUOTE_NONE):
            if r.get('status') in wanted:
                rows.append(r)
    if not rows:
        print(f'nothing to do: no {"/".join(sorted(wanted))} rows in {a.results}')
        sys.exit(0)

    import getpass
    password = os.environ.get('P') or (getpass.getpass(f'App password for {user}: ') if sys.stdin.isatty() else '')
    if not password:
        die('no app password: set P or run interactively')

    out = a.out or f'/var/tmp/nc-fix-{time.strftime("%Y%m%d-%H%M%S")}'
    os.makedirs(out, mode=0o700, exist_ok=True)
    os.chmod(out, 0o700)
    if a.backup:
        os.makedirs(a.backup, mode=0o700, exist_ok=True)
        os.chmod(a.backup, 0o700)
    dav = FixDav(base, user, password, out)
    dav.h1_timeout = a.h1_timeout
    try:
        counts = {}
        for s in sorted(wanted):
            counts[s] = sum(1 for r in rows if r['status'] == s)
        print('to fix: ' + ', '.join(f'{n} {s}' for s, n in counts.items()))
        if a.apply:
            v = dav.versioning()
            print({True: 'Versions app: enabled - the previous content of each file is kept as a version.',
                   False: 'Versions app: NOT enabled - the previous content is NOT kept anywhere.',
                   None: 'Versions app: could not be determined.'}[v])
            print('Versions are not a backup: they expire by age, are removed when they use too much of the '
                  'quota, and go with the file when it is deleted.')
            if a.backup:
                print(f'Server copies are saved to {a.backup} before overwriting (plaintext).')
            if 'CONTENT_DIFF' in wanted and v is not True and not a.backup and not a.yes:
                die('refusing to overwrite CONTENT_DIFF files without versions and without --backup '
                    '(add --backup DIR, or --yes if you really mean it)')
            if not a.yes:
                if not sys.stdin.isatty():
                    die('--apply needs confirmation: run interactively or add --yes')
                ans = input(f'Overwrite {len(rows)} files on {base} as {user}? Type "yes": ')
                if ans.strip() != 'yes':
                    die('not confirmed, nothing changed')
        else:
            print('dry run: nothing is uploaded (add --apply)')

        res = open(os.path.join(out, 'fix-results.tsv'), 'w', encoding='utf-8')
        res.write('status\thttp\tfileid\tdetail\tpath\n')
        result_counts = {}

        def emit(status, rel, http='-', fid='-', detail='-'):
            res.write(f'{status}\t{http}\t{fid}\t{detail}\t{rel}\n')
            res.flush()
            result_counts[status] = result_counts.get(status, 0) + 1

        for i, r in enumerate(rows, 1):
            rel = r['path']
            lp = os.path.join(local, rel)
            if not os.path.isfile(lp):
                emit('SKIP_LOCAL_MISSING', rel)
                continue
            lmd5 = local_md5(lp)
            if lmd5 != r['local_md5']:
                emit('SKIP_LOCAL_CHANGED', rel, detail=f'now={lmd5} compared={r["local_md5"]}')
                continue
            save = os.path.join(a.backup, rel) if (a.backup and a.apply) else None
            if save:
                os.makedirs(os.path.dirname(save), exist_ok=True)
            ok, rmd5 = dav.download(rel, r['remote_md5'], save)
            if not ok:
                emit('SKIP_REMOTE_CHANGED', rel, detail=f'now={rmd5} compared={r["remote_md5"]}')
                continue
            id1 = dav.fileid(rel)
            if not a.apply:
                emit('WOULD_FIX', rel, fid=id1, detail=r['status'])
                continue
            code = dav.put(rel, lp)
            if not code.startswith('2'):
                emit('PUT_FAIL', rel, code, id1)
                continue
            id2 = dav.fileid(rel)
            if not id1 or id1 != id2:
                emit('ID_CHANGED', rel, code, f'{id1}->{id2}')
                continue
            rsize, lsize = dav.size(rel), os.path.getsize(lp)
            if rsize != lsize:
                emit('SIZE_STILL_WRONG', rel, code, id2, f'reported={rsize} local={lsize}')
                continue
            if dav.strict_md5(rel) != lmd5:
                emit('VERIFY_FAIL', rel, code, id2)
                continue
            emit('FIXED', rel, code, id2, r['status'])
            if i % 25 == 0:
                print(f'  {i}/{len(rows)}', file=sys.stderr)
        res.close()
    finally:
        dav.close()

    print('== summary')
    for s, n in sorted(result_counts.items(), key=lambda x: -x[1]):
        print(f'{n:8d} {s}')
    print(f'results: {out}/fix-results.tsv')
    sys.exit(0 if set(result_counts) <= {'FIXED', 'WOULD_FIX'} else 1)


if __name__ == '__main__':
    main()
