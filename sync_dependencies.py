#!/usr/bin/env python3
import os
import sys
import argparse
import requests
import toml
import subprocess
import json
from urllib.parse import urlparse

def parse_args():
    parser = argparse.ArgumentParser(description="Sync buildpack dependencies to a Docker registry using oras.")
    parser.add_argument('--buildpack-toml', default='buildpack.toml', help='Path to buildpack.toml')
    parser.add_argument('--registry', required=True, help='Target docker registry, e.g. artifactory.example.com/repo1/pkgs')
    parser.add_argument('--username', help='Registry username (optional)')
    parser.add_argument('--password', help='Registry password (optional)')
    parser.add_argument('--temp-dir', default='tmp_downloads', help='Temp dir for downloads')
    parser.add_argument('--rewrite-toml', action='store_true', help='Rewrite buildpack.toml with new uris to buildpack-modified.toml')
    parser.add_argument('--download-only', action='store_true', help='Only download dependencies and generate meta file')
    parser.add_argument('--upload-only', action='store_true', help='Only upload dependencies from meta file and rewrite toml if needed')
    return parser.parse_args()

def get_dependencies(toml_path):
    data = toml.load(toml_path)
    deps = data.get('metadata', {}).get('dependencies', [])
    return deps

def download_file(url, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    filename = os.path.basename(urlparse(url).path)
    dest_path = os.path.join(dest_dir, filename)
    if os.path.exists(dest_path):
        print(f"File already exists: {dest_path}")
        return dest_path
    print(f"Downloading {url} ...")
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        with open(dest_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    print(f"Downloaded to {dest_path}")
    return dest_path

def push_with_oras(registry, file_path, username=None, password=None):
    filename = os.path.basename(file_path)
    ref = f"{registry}:{filename}"
    cmd = ["oras", "push", ref, file_path]
    env = os.environ.copy()
    if username and password:
        env["ORAS_USERNAME"] = username
        env["ORAS_PASSWORD"] = password
    print(f"Pushing {file_path} to {ref} ...")
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        print(f"Failed to push {file_path} to {ref}", file=sys.stderr)
        sys.exit(1)
    print(f"Pushed {file_path} to {ref}")

def download_all(deps, temp_dir, meta_path):
    meta = []
    for dep in deps:
        uri = dep.get('uri')
        if not uri:
            continue
        file_path = download_file(uri, temp_dir)
        meta.append({'uri': uri, 'file_path': file_path})
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, indent=2)
    print(f"Download meta written to {meta_path}")

def upload_all(meta_path, registry, username=None, password=None):
    with open(meta_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)
    for entry in meta:
        file_path = entry['file_path']
        push_with_oras(registry, file_path, username, password)

def rewrite_toml(orig_path, new_path, registry, deps):
    """
    生成新的 buildpack-modified.toml，将所有同步过的 uri 替换为 registry 地址。
    """
    with open(orig_path, 'r', encoding='utf-8') as f:
        orig_data = toml.load(f)
    # 只替换 metadata.dependencies 下的 uri
    for i, dep in enumerate(orig_data.get('metadata', {}).get('dependencies', [])):
        uri = dep.get('uri')
        if uri:
            filename = os.path.basename(urlparse(uri).path)
            dep['uri'] = f"{registry}:{filename}"
    with open(new_path, 'w', encoding='utf-8') as f:
        toml.dump(orig_data, f)
    print(f"Rewritten TOML written to {new_path}")

def rewrite_toml_from_meta(orig_path, new_path, registry, meta_path):
    with open(orig_path, 'r', encoding='utf-8') as f:
        orig_data = toml.load(f)
    with open(meta_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)
    uri_map = {entry['uri']: f"{registry}:{os.path.basename(entry['file_path'])}" for entry in meta}
    for dep in orig_data.get('metadata', {}).get('dependencies', []):
        uri = dep.get('uri')
        if uri and uri in uri_map:
            dep['uri'] = uri_map[uri]
    with open(new_path, 'w', encoding='utf-8') as f:
        toml.dump(orig_data, f)
    print(f"Rewritten TOML written to {new_path}")

def main():
    args = parse_args()
    meta_path = os.path.join(args.temp_dir, 'downloaded_files.json')
    if args.download_only and args.upload_only:
        print("--download-only 和 --upload-only 不能同时使用", file=sys.stderr)
        sys.exit(1)
    if args.download_only:
        deps = get_dependencies(args.buildpack_toml)
        download_all(deps, args.temp_dir, meta_path)
        return
    if args.upload_only:
        upload_all(meta_path, args.registry, args.username, args.password)
        if args.rewrite_toml:
            rewrite_toml_from_meta(args.buildpack_toml, 'buildpack-modified.toml', args.registry, meta_path)
        return
    # 默认全流程
    deps = get_dependencies(args.buildpack_toml)
    download_all(deps, args.temp_dir, meta_path)
    upload_all(meta_path, args.registry, args.username, args.password)
    if args.rewrite_toml:
        rewrite_toml_from_meta(args.buildpack_toml, 'buildpack-modified.toml', args.registry, meta_path)

if __name__ == "__main__":
    main()
