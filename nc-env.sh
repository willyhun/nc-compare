# SPDX-License-Identifier: GPL-3.0-or-later
# nc-env.sh - settings for nc-compare.py. Source it, don't run it:
#   . ./nc-env.sh && ./nc-compare.py
#
# Put your own values in nc-env.local.sh next to this file (never share that file):
#   export NC=https://cloud.example.com
#   export U=alice
#   export P='xxxxx-xxxxx-xxxxx-xxxxx-xxxxx'   # app password; or leave P out and get prompted
#   export LOCAL="$HOME/Nextcloud"
# and protect it:  chmod 600 nc-env.local.sh
#
# NC     Nextcloud base URL, including a sub-path if Nextcloud is not at the web root
# U      login name of the account whose files are compared
# P      app password of that account (see README.md); prompted when unset
# LOCAL  local folder that mirrors the account's files root (e.g. the desktop client sync folder)

_nc_env_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [ -f "$_nc_env_dir/nc-env.local.sh" ]; then . "$_nc_env_dir/nc-env.local.sh"; fi
unset _nc_env_dir

: "${NC:=https://cloud.example.com}"
: "${U:=}"
: "${LOCAL:=}"
export NC U LOCAL
if [ -n "${P:-}" ]; then export P; fi
