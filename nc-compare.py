#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The nc-compare authors
#
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version. It is distributed WITHOUT ANY WARRANTY; see the
# LICENSE file or <https://www.gnu.org/licenses/>.
"""
nc-compare.py - compare files stored in Nextcloud with a local copy. Read-only.

For every file it checks:
  * the size Nextcloud reports (WebDAV getcontentlength, i.e. oc_filecache)
    against the real size of the local copy, and
  * the content: the file is downloaded and its MD5 compared with the local copy
    (skip with --size-only).

Why: a file whose recorded size is wrong still decrypts and downloads, but the
server announces the wrong Content-Length. Desktop clients then fail with
"An unexpected error occurred" or "The start offset (N) exceeded the size of the
entity (M)", and the Nextcloud log shows nothing. This happened after upgrading
an old instance with server-side encryption: files written in the old base64
format kept their inflated size in the database. occ files:scan,
maintenance:repair and encryption checks did not notice.

Downloads use curl, HTTP/2 when the server offers it. A wrong recorded size makes
an HTTP/2 transfer end with a stream error (curl exit 92) although the complete
content arrived; if the MD5 still matches the local copy the file counts as
received. Otherwise the download is retried once over HTTP/1.1 with
--ignore-content-length (slow: curl then waits for the server to close the
connection, bounded by --h1-timeout).

Two ways to choose files:
  --list FILE   paths relative to the user's files root, one per line
                ("/<user>/files/" prefixes and leading slashes are accepted)
  (no --list)   walk the whole tree under --dir on the server and under LOCAL,
                and also report files that exist on only one side

Settings come from the environment (see nc-env.sh):
  NC     Nextcloud base URL, e.g. https://cloud.example.com or https://example.com/nextcloud
  U      Nextcloud login name of the account whose files are compared
  P      app password of that account (prompted if unset; never put it on a command line)
  LOCAL  local folder that mirrors the user's files root (e.g. the desktop client's sync folder)

Credentials: see README.md, "Account and app password".

Output: <out>/results.tsv (default out: /var/tmp/nc-compare-<timestamp>, mode 700)
  status  transport  curl_exit  http  reported_size  local_size  received  local_md5  remote_md5  path
Status values:
  OK              size and content match
  SIZE_WRONG      content identical, but the recorded size is wrong -> clients will fail on it
  CONTENT_DIFF    content differs
  DL_FAIL         download failed (HTTP error) or stayed incomplete
  SIZE_DIFF       (--size-only) recorded size differs from the local size
  MISSING_LOCAL   on the server, not in LOCAL
  MISSING_REMOTE  in LOCAL (or in the list), not on the server
Exit code: 0 all OK, 1 differences found, 2 usage/setup error.
"""
import argparse, fnmatch, getpass, hashlib, os, subprocess, sys, tempfile, threading, time
import unicodedata, urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

DAV = '{DAV:}'
# Files the desktop client keeps for itself or never syncs; matched against the base name.
DEFAULT_EXCLUDES = ['.sync_*.db', '.sync_*.db-*', '._sync_*.db', '._sync_*.db-*', '.csync_journal.db*',
                    '.owncloudsync.log*', '.nextcloudsync.log*', '.sync-exclude.lst',
                    '.~lock.*', '~$*', 'desktop.ini', 'Desktop.ini', 'Thumbs.db', '.DS_Store']
PROPFIND_BODY = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
                 '<d:getcontentlength/><d:resourcetype/></d:prop></d:propfind>')


def die(msg):
    print(f'nc-compare: {msg}', file=sys.stderr)
    sys.exit(2)


def nfc(s):
    return unicodedata.normalize('NFC', s)


class Dav:
    def __init__(self, base, user, password, workdir):
        u = urllib.parse.urlparse(base.rstrip('/'))
        if u.scheme not in ('http', 'https') or not u.netloc:
            die(f'NC is not a URL: {base!r}')
        self.origin = f'{u.scheme}://{u.netloc}'
        self.root = u.path + '/remote.php/dav/files/' + urllib.parse.quote(user, safe='') + '/'
        self.root_unq = urllib.parse.unquote(self.root)
        # Credentials go to curl through a config file (mode 600), not through argv,
        # so they don't show up in the process list.
        fd, self.cfg = tempfile.mkstemp(prefix='.curl-', suffix='.cfg', dir=workdir)
        esc = lambda s: s.replace('\\', '\\\\').replace('"', '\\"')
        with os.fdopen(fd, 'w') as f:
            f.write(f'user = "{esc(user)}:{esc(password)}"\n')
        os.chmod(self.cfg, 0o600)

    def close(self):
        try:
            os.unlink(self.cfg)
        except OSError:
            pass

    def url(self, rel):
        return self.origin + self.root + urllib.parse.quote(rel, safe='/')

    def _curl(self):
        return ['curl', '-q', '-K', self.cfg, '-g', '-s']

    def propfind(self, rel, depth):
        """Returns {rel: size or None for folders}, or None if the path does not exist."""
        cmd = self._curl() + ['-X', 'PROPFIND', '-H', f'Depth: {depth}',
                              '-H', 'Content-Type: application/xml', '--data', PROPFIND_BODY,
                              '--max-time', '300', '-w', '%{stderr}%{http_code}\n', self.url(rel)]
        p = subprocess.run(cmd, capture_output=True)
        code = (p.stderr.decode(errors='replace').strip().splitlines() or ['000'])[-1]
        if code == '404':
            return None
        if code == '401':
            die('authentication failed (HTTP 401): check U and P')
        if code != '207':
            raise RuntimeError(f'PROPFIND {rel!r}: HTTP {code}, curl exit {p.returncode}')
        out = {}
        for r in ET.fromstring(p.stdout).findall(DAV + 'response'):
            href = urllib.parse.unquote(r.findtext(DAV + 'href') or '')
            if not href.startswith(self.root_unq):
                continue
            path = href[len(self.root_unq):].strip('/')
            prop = None
            for ps in r.findall(DAV + 'propstat'):
                if ' 200 ' in (ps.findtext(DAV + 'status') or ''):
                    prop = ps.find(DAV + 'prop')
            if prop is None:
                continue
            is_dir = prop.find(f'{DAV}resourcetype/{DAV}collection') is not None
            length = prop.findtext(DAV + 'getcontentlength')
            out[path] = None if is_dir else (int(length) if length else 0)
        return out

    def get(self, rel, extra=()):
        """Downloads to a hash. Returns (curl_exit, http_code, http_version, bytes, md5)."""
        cmd = self._curl() + ['-f'] + list(extra) + [
            '-w', '%{stderr}\n%{http_code} %{http_version} %{size_download}\n', self.url(rel)]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        h, n = hashlib.md5(), 0
        for chunk in iter(lambda: p.stdout.read(1 << 20), b''):
            h.update(chunk)
            n += len(chunk)
        err = p.stderr.read().decode(errors='replace')
        rc = p.wait()
        parts = (err.strip().splitlines() or ['000 0 0'])[-1].split()
        code, ver = (parts + ['000', '0'])[:2]
        return rc, code, ver, n, h.hexdigest()


def local_md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def walk_remote(dav, top):
    files, todo = {}, [top.strip('/')]
    while todo:
        d = todo.pop()
        listing = dav.propfind(d, 1)
        if listing is None:
            if d == top.strip('/'):
                die(f'remote folder not found: {top!r}')
            continue
        for path, size in listing.items():
            if path == d:
                continue
            if size is None:
                todo.append(path)
            else:
                files[nfc(path)] = (path, size)
    return files


def walk_local(local, top):
    base = os.path.join(local, top.strip('/'))
    files = {}
    for dirpath, dirnames, filenames in os.walk(base):
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, local)
            files[nfc(rel)] = rel
    return files


def excluded(rel, patterns):
    name = rel.rsplit('/', 1)[-1]
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def main():
    ap = argparse.ArgumentParser(description='Compare Nextcloud files with a local copy (read-only).',
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('--list', help='file with paths to check (relative to the files root)')
    ap.add_argument('--dir', default='', help='folder to walk when no --list is given (default: everything)')
    ap.add_argument('--out', help='output folder (default: /var/tmp/nc-compare-<timestamp>)')
    ap.add_argument('--size-only', action='store_true', help='compare recorded size only, no downloads')
    ap.add_argument('-j', '--jobs', type=int, default=2, help='parallel downloads (default 2)')
    ap.add_argument('--h1-timeout', type=int, default=300,
                    help='max seconds per HTTP/1.1 fallback download (default 300)')
    ap.add_argument('--exclude', action='append', default=[], help='extra base-name pattern to skip')
    ap.add_argument('--no-default-excludes', action='store_true', help='do not skip sync-client files')
    a = ap.parse_args()

    base, user, local = os.environ.get('NC', ''), os.environ.get('U', ''), os.environ.get('LOCAL', '')
    if not base or 'example.com' in base:
        die('set NC to the Nextcloud base URL (see nc-env.sh)')
    if not user:
        die('set U to the Nextcloud login name')
    if not local or not os.path.isdir(local):
        die(f'set LOCAL to an existing local folder (now: {local!r})')
    password = os.environ.get('P') or (getpass.getpass(f'App password for {user}: ') if sys.stdin.isatty() else '')
    if not password:
        die('no app password: set P or run interactively')
    if subprocess.run(['curl', '--version'], capture_output=True).returncode != 0:
        die('curl not found')

    out = a.out or f'/var/tmp/nc-compare-{time.strftime("%Y%m%d-%H%M%S")}'
    os.makedirs(out, mode=0o700, exist_ok=True)
    os.chmod(out, 0o700)  # results contain file names
    patterns = ([] if a.no_default_excludes else DEFAULT_EXCLUDES) + a.exclude
    dav = Dav(base, user, password, out)
    try:
        # items: (key, remote_path or None, reported_size or None, local_path or None)
        items = []
        if a.list:
            prefix = f'/{user}/files/'
            with open(a.list, encoding='utf-8') as f:
                rels = []
                for line in f:
                    rel = line.rstrip('\r\n')
                    if not rel.strip():
                        continue
                    if rel.startswith(prefix):
                        rel = rel[len(prefix):]
                    rels.append(rel.lstrip('/'))

            def stat_one(rel):
                info = dav.propfind(rel, 0)
                size = None if info is None else next(iter(info.values()), None)
                lp = os.path.join(local, rel)
                return (nfc(rel), rel if info is not None else None, size, lp if os.path.isfile(lp) else None)
            with ThreadPoolExecutor(max(1, a.jobs) * 2) as ex:
                items = list(ex.map(stat_one, rels))
        else:
            print(f'listing server folder {a.dir or "/"} ...', file=sys.stderr)
            remote = {k: v for k, v in walk_remote(dav, a.dir).items() if not excluded(k, patterns)}
            loc = {k: v for k, v in walk_local(local, a.dir).items() if not excluded(k, patterns)}
            for k in sorted(set(remote) | set(loc)):
                r = remote.get(k)
                items.append((k, r[0] if r else None, r[1] if r else None,
                              os.path.join(local, loc[k]) if k in loc else None))
        print(f'{len(items)} files to compare, results in {out}/results.tsv', file=sys.stderr)

        lock, counts, done = threading.Lock(), {}, [0]
        res = open(os.path.join(out, 'results.tsv'), 'w', encoding='utf-8')
        res.write('status\ttransport\tcurl_exit\thttp\treported_size\tlocal_size\treceived\tlocal_md5\tremote_md5\tpath\n')
        t0 = [time.time()]

        def emit(status, key, *cols):
            row = [status] + [str(c) for c in cols] + [key]
            with lock:
                res.write('\t'.join(row) + '\n')
                res.flush()
                counts[status] = counts.get(status, 0) + 1
                done[0] += 1
                if time.time() - t0[0] > 10:
                    t0[0] = time.time()
                    print(f'  {done[0]}/{len(items)}  ' + '  '.join(f'{s}={n}' for s, n in sorted(counts.items())),
                          file=sys.stderr)

        def check(item):
            key, rpath, rsize, lpath = item
            if rpath is None:
                return emit('MISSING_REMOTE', key, '-', '-', '-', '-', os.path.getsize(lpath) if lpath else '-', '-', '-', '-')
            if lpath is None:
                return emit('MISSING_LOCAL', key, '-', '-', '-', rsize, '-', '-', '-', '-')
            lsize = os.path.getsize(lpath)
            if a.size_only:
                return emit('OK' if rsize == lsize else 'SIZE_DIFF', key, '-', '-', '-', rsize, lsize, '-', '-', '-')
            lmd5 = local_md5(lpath)
            rc, code, ver, n, rmd5 = dav.get(rpath)
            transport = 'h2' if ver == '2' else f'http/{ver}'
            if rc not in (0, 22) and rmd5 != lmd5:
                # e.g. HTTP/2 stream reset before the full content arrived: try HTTP/1.1
                rc, code, ver, n, rmd5 = dav.get(rpath, ['--http1.1', '--ignore-content-length',
                                                         '--max-time', str(a.h1_timeout)])
                transport = 'h1-fallback'
            elif rc != 0 and rmd5 == lmd5:
                transport += '-incomplete-ok'   # error at the end, but the full content arrived
            if rmd5 == lmd5:
                status = 'OK' if rsize == lsize else 'SIZE_WRONG'
            elif rc == 22 or (rc != 0 and rc != 28):
                status = 'DL_FAIL'
            elif rc == 28 and n < lsize:
                status = 'DL_FAIL'
            else:
                status = 'CONTENT_DIFF'
            emit(status, key, transport, rc, code, rsize, lsize, n, lmd5, rmd5)

        with ThreadPoolExecutor(max(1, a.jobs)) as ex:
            list(ex.map(check, items))
        res.close()
    finally:
        dav.close()

    print('== summary', file=sys.stderr)
    for s, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f'{n:8d} {s}')
    print(f'results: {out}/results.tsv')
    sys.exit(0 if set(counts) <= {'OK'} else 1)


if __name__ == '__main__':
    main()
