#!/usr/bin/env python3
import os
import sys
import argparse
import requests
import toml
import subprocess
import json
from urllib.parse import urlparse
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue, Empty

TASK_LOG_FILENAME = "task_log.json"
TASK_STATUS_PENDING = "pending"
TASK_STATUS_DOWNLOADING = "downloading"
TASK_STATUS_DOWNLOADED = "downloaded"
TASK_STATUS_UPLOADING = "uploading"
TASK_STATUS_UPLOADED = "uploaded"
TASK_STATUS_FAILED = "failed"

DEFAULT_WORKERS = 4
log_lock = threading.Lock()


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

def push_with_curl(registry, file_path, username=None, password=None):
    """
    使用 curl 上传文件到 registry。
    registry: 例如 https://registry.example.com:20030/repo1
    file_path: 本地文件路径
    username/password: 认证信息，可选
    """
    filename = os.path.basename(file_path)
    # 构造目标 URL
    if not registry.startswith("http://") and not registry.startswith("https://"):
        url = f"https://{registry}/{filename}"
    else:
        url = f"{registry.rstrip('/')}/{filename}"
    cmd = ["curl", "-T", file_path, url]
    if username and password:
        cmd.insert(1, f"-u{username}:{password}")
    print(f"Uploading {file_path} to {url} ...")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"Failed to upload {file_path} to {url}", file=sys.stderr)
        sys.exit(1)
    print(f"Uploaded {file_path} to {url}")

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
        push_with_curl(registry, file_path, username, password)

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

def rewrite_toml_from_meta(orig_path, new_path, registry, temp_dir):
    """
    根据 task_log.json 或 downloaded_files.json 重写 buildpack.toml。
    """
    import glob
    with open(orig_path, 'r', encoding='utf-8') as f:
        orig_data = toml.load(f)
    # 优先读取 task_log.json
    log_path = os.path.join(temp_dir, TASK_LOG_FILENAME)
    meta = None
    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8') as f:
            meta = json.load(f)
        uri_map = {entry['uri']: entry.get('upload_url') or f"{registry}:{os.path.basename(entry['file_path'])}" for entry in meta if entry.get('status') == TASK_STATUS_UPLOADED or entry.get('status') == TASK_STATUS_DOWNLOADED}
    else:
        # 兼容旧的 downloaded_files.json
        meta_path = os.path.join(temp_dir, 'downloaded_files.json')
        if os.path.exists(meta_path):
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            uri_map = {entry['uri']: f"{registry}:{os.path.basename(entry['file_path'])}" for entry in meta}
        else:
            print(f"No task_log.json or downloaded_files.json found in {temp_dir}", file=sys.stderr)
            return
    for dep in orig_data.get('metadata', {}).get('dependencies', []):
        uri = dep.get('uri')
        if uri and uri in uri_map:
            dep['uri'] = uri_map[uri]
    with open(new_path, 'w', encoding='utf-8') as f:
        toml.dump(orig_data, f)
    print(f"Rewritten TOML written to {new_path}")

def load_task_log(temp_dir):
    log_path = os.path.join(temp_dir, TASK_LOG_FILENAME)
    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def save_task_log(temp_dir, log):
    log_path = os.path.join(temp_dir, TASK_LOG_FILENAME)
    with open(log_path, 'w', encoding='utf-8') as f:
        json.dump(log, f, indent=2)


def init_task_log(deps, temp_dir, registry):
    os.makedirs(temp_dir, exist_ok=True)
    log = []
    for dep in deps:
        uri = dep.get('uri')
        if not uri:
            continue
        filename = os.path.basename(urlparse(uri).path)
        if not registry.startswith("http://") and not registry.startswith("https://"):
            upload_url = f"https://{registry}/{filename}"
        else:
            upload_url = f"{registry.rstrip('/')}/{filename}"
        log.append({
            'uri': uri,
            'file_path': os.path.join(temp_dir, filename),
            'upload_url': upload_url,
            'status': TASK_STATUS_PENDING,
            'error': None
        })
    save_task_log(temp_dir, log)
    return log


def thread_safe_update_task_status(log, uri, status, error=None):
    with log_lock:
        for entry in log:
            if entry['uri'] == uri:
                entry['status'] = status
                entry['error'] = error
                break

def thread_safe_save_task_log(temp_dir, log):
    with log_lock:
        save_task_log(temp_dir, log)

def download_worker(entry, temp_dir, log):
    uri = entry['uri']
    file_path = entry['file_path']
    status = entry.get('status', TASK_STATUS_PENDING)
    if status == TASK_STATUS_UPLOADED or status == TASK_STATUS_DOWNLOADED:
        return
    thread_safe_update_task_status(log, uri, TASK_STATUS_DOWNLOADING)
    thread_safe_save_task_log(temp_dir, log)
    try:
        if not (os.path.exists(file_path) and os.path.getsize(file_path) > 0):
            download_file(uri, temp_dir)
        thread_safe_update_task_status(log, uri, TASK_STATUS_DOWNLOADED)
    except Exception as e:
        thread_safe_update_task_status(log, uri, TASK_STATUS_FAILED, str(e))
    thread_safe_save_task_log(temp_dir, log)

def upload_worker(entry, registry, username, password, temp_dir, log):
    uri = entry['uri']
    file_path = entry['file_path']
    status = entry.get('status', TASK_STATUS_DOWNLOADED)
    if status == TASK_STATUS_UPLOADED:
        return
    if not os.path.exists(file_path):
        return
    thread_safe_update_task_status(log, uri, TASK_STATUS_UPLOADING)
    thread_safe_save_task_log(temp_dir, log)
    try:
        push_with_curl(registry, file_path, username, password)
        thread_safe_update_task_status(log, uri, TASK_STATUS_UPLOADED)
    except Exception as e:
        thread_safe_update_task_status(log, uri, TASK_STATUS_FAILED, str(e))
    thread_safe_save_task_log(temp_dir, log)

def download_and_upload_all(deps, temp_dir, registry, username=None, password=None, max_workers=DEFAULT_WORKERS):
    log = load_task_log(temp_dir)
    if not log:
        log = init_task_log(deps, temp_dir, registry)
    upload_queue = Queue()
    # 启动上传线程池
    def upload_consumer():
        while True:
            try:
                entry = upload_queue.get(timeout=3)
            except Empty:
                break
            upload_worker(entry, registry, username, password, temp_dir, log)
            upload_queue.task_done()
    upload_threads = []
    for _ in range(max_workers):
        t = threading.Thread(target=upload_consumer)
        t.daemon = True
        t.start()
        upload_threads.append(t)
    # 启动下载线程池，下载完成即入上传队列
    def download_and_enqueue(entry):
        uri = entry['uri']
        file_path = entry['file_path']
        status = entry.get('status', TASK_STATUS_PENDING)
        if status == TASK_STATUS_UPLOADED:
            return
        if status == TASK_STATUS_DOWNLOADED:
            # 已下载未上传的，直接入队
            upload_queue.put(entry)
            return
        thread_safe_update_task_status(log, uri, TASK_STATUS_DOWNLOADING)
        thread_safe_save_task_log(temp_dir, log)
        try:
            if not (os.path.exists(file_path) and os.path.getsize(file_path) > 0):
                download_file(uri, temp_dir)
            thread_safe_update_task_status(log, uri, TASK_STATUS_DOWNLOADED)
            thread_safe_save_task_log(temp_dir, log)
            upload_queue.put(entry)
        except Exception as e:
            thread_safe_update_task_status(log, uri, TASK_STATUS_FAILED, str(e))
            thread_safe_save_task_log(temp_dir, log)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(download_and_enqueue, entry)
                   for entry in log if entry.get('status') not in [TASK_STATUS_UPLOADED]]
        for f in as_completed(futures):
            pass
    # 等待所有上传完成
    upload_queue.join()

def main():
    args = parse_args()
    meta_path = os.path.join(args.temp_dir, 'downloaded_files.json')
    if args.download_only and args.upload_only:
        print("--download-only 和 --upload-only 不能同时使用", file=sys.stderr)
        sys.exit(1)
    deps = get_dependencies(args.buildpack_toml)
    log = load_task_log(args.temp_dir)
    # 检查是否所有都已上传
    all_uploaded = log and all(entry.get('status') == TASK_STATUS_UPLOADED for entry in log)
    if args.rewrite_toml and all_uploaded:
        rewrite_toml_from_meta(args.buildpack_toml, 'buildpack-modified.toml', args.registry, args.temp_dir)
        return
    if args.download_only:
        # 只下载，更新任务日志
        if not log:
            log = init_task_log(deps, args.temp_dir, args.registry)
        for entry in log:
            uri = entry['uri']
            file_path = entry['file_path']
            status = entry.get('status', TASK_STATUS_PENDING)
            if status == TASK_STATUS_UPLOADED or status == TASK_STATUS_DOWNLOADED:
                continue
            thread_safe_update_task_status(log, uri, TASK_STATUS_DOWNLOADING)
            thread_safe_save_task_log(args.temp_dir, log)
            try:
                if not (os.path.exists(file_path) and os.path.getsize(file_path) > 0):
                    download_file(uri, args.temp_dir)
                thread_safe_update_task_status(log, uri, TASK_STATUS_DOWNLOADED)
            except Exception as e:
                thread_safe_update_task_status(log, uri, TASK_STATUS_FAILED, str(e))
            thread_safe_save_task_log(args.temp_dir, log)
        return
    if args.upload_only:
        # 只上传，更新任务日志
        for entry in log:
            uri = entry['uri']
            file_path = entry['file_path']
            status = entry.get('status', TASK_STATUS_DOWNLOADED)
            if status == TASK_STATUS_UPLOADED:
                continue
            if not os.path.exists(file_path):
                continue
            thread_safe_update_task_status(log, uri, TASK_STATUS_UPLOADING)
            thread_safe_save_task_log(args.temp_dir, log)
            try:
                push_with_curl(args.registry, file_path, args.username, args.password)
                thread_safe_update_task_status(log, uri, TASK_STATUS_UPLOADED)
            except Exception as e:
                thread_safe_update_task_status(log, uri, TASK_STATUS_FAILED, str(e))
            thread_safe_save_task_log(args.temp_dir, log)
        if args.rewrite_toml:
            rewrite_toml_from_meta(args.buildpack_toml, 'buildpack-modified.toml', args.registry, args.temp_dir)
        return
    # 默认全流程，边下载边上传
    download_and_upload_all(deps, args.temp_dir, args.registry, args.username, args.password)
    if args.rewrite_toml:
        rewrite_toml_from_meta(args.buildpack_toml, 'buildpack-modified.toml', args.registry, args.temp_dir)

if __name__ == "__main__":
    main()
