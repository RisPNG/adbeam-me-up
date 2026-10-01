#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"
unset PYTHONHOME PYTHONPATH VIRTUAL_ENV
stage_arguments=()
hidden=false
for argument in "$@"; do
    case "$argument" in
        --hidden) hidden=true; stage_arguments+=("$argument") ;;
        --no-launch) stage_arguments+=("$argument") ;;
        --help)
            printf '%s\n' 'Usage: setup_linux.sh [--hidden] [--no-launch]' 'Prepare the portable Python runtime, repair the environment and shortcuts, then launch the app.'
            exit 0
            ;;
        *) printf 'Unknown option: %s\n' "$argument" >&2; exit 2 ;;
    esac
done
if "$hidden"; then
    mkdir -p -- "$root/bin/linux/logs"
    exec >> "$root/bin/linux/logs/setup.log" 2>&1
fi
if [[ "$(uname -m)" != "x86_64" ]]; then
    printf '%s\n' 'The pinned MsPy 3.11.14 Linux runtime supports x86_64 only.' >&2
    exit 1
fi
runtime="$root/int/linux/MsPy-3_11_14"
python="$runtime/bin/python3.11"
if [[ ! -x "$python" ]] || ! "$python" -c 'import sys; assert sys.version_info[:3] == (3, 11, 14)' >/dev/null 2>&1; then
    if ! command -v unzip >/dev/null 2>&1; then
        printf '%s\n' 'Install unzip using your system software manager, then run setup again.' >&2
        exit 1
    fi
    if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
        printf '%s\n' 'Setup requires sha256sum or shasum to verify the runtime download.' >&2
        exit 1
    fi
    mkdir -p -- "$root/int/linux"
    staging="$(mktemp -d "$root/int/linux/.mspy-bootstrap.XXXXXX")"
    trap 'rm -rf -- "$staging"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    archive="$staging/MsPy-3_11_14-linux.zip"
    url='https://github.com/RisPNG/MsPy/releases/download/3.11.14/MsPy-3_11_14-linux.zip'
    expected='abd329f7b29c7c62b780942cec1b8e79a49e371f848098bdd4ab796391d2a851'
    printf '%s\n' 'Downloading portable Python 3.11.14...'
    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --retry 2 --connect-timeout 20 --max-time 300 --output "$archive" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget --timeout=20 --tries=3 --output-document="$archive" "$url"
    else
        printf '%s\n' 'Setup requires curl or wget to download portable Python.' >&2
        exit 1
    fi
    if command -v sha256sum >/dev/null 2>&1; then
        checksum="$(sha256sum < "$archive")"
    else
        checksum="$(shasum -a 256 < "$archive")"
    fi
    if [[ "${checksum%% *}" != "$expected" ]]; then
        printf '%s\n' 'The runtime download did not match its published SHA-256 digest. Run setup again.' >&2
        exit 1
    fi
    unzip -q "$archive" -d "$staging"
    extracted="$staging/MsPy-3_11_14/bin/python3.11"
    if [[ ! -f "$extracted" ]]; then
        printf '%s\n' 'The runtime archive did not contain the expected Python interpreter.' >&2
        exit 1
    fi
    chmod +x -- "$extracted"
    "$extracted" -c 'import sys; assert sys.version_info[:3] == (3, 11, 14)'
    if [[ -e "$runtime" ]]; then
        mv -- "$runtime" "$staging/previous-runtime"
    fi
    if ! mv -- "$staging/MsPy-3_11_14" "$runtime"; then
        if [[ -d "$staging/previous-runtime" ]]; then
            mv -- "$staging/previous-runtime" "$runtime"
        fi
        exit 1
    fi
    rm -rf -- "$staging"
    trap - EXIT INT TERM
fi
"$python" -u "$root/tools/setup_portable.py" "${stage_arguments[@]}"
