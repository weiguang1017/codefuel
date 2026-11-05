"""Resolve source versions from Git tags and freeze that version into distributable apps."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

BUILD_INFO_FILE = 'build-info.json'
VERSION_PATTERN = re.compile(r'(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)'
                             r'(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?'
                             r'(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?')


def normalize_version(value):
    if not isinstance(value, str):
        raise ValueError('版本号必须是字符串')
    version = value.removeprefix('v')
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError('版本必须采用 v1.2.3 或 v1.2.3-rc.1 这样的格式')
    return version


def _git(root, *args):
    try:
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True,
                                timeout=3, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def get_version_info(root=None, *, frozen=None, environ=None):
    frozen = bool(getattr(sys, 'frozen', False)) if frozen is None else frozen
    root = Path(root) if root is not None else (Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
                                               if frozen else Path(__file__).resolve().parents[1])
    if frozen:
        # A distributed app must never read tags from the user's current folder or environment.
        try:
            info = json.loads((root / BUILD_INFO_FILE).read_text(encoding='utf-8'))
            version = normalize_version(info['version'])
            if info.get('tag') and normalize_version(info['tag']) != version:
                raise ValueError('tag mismatch')
            return {**info, 'version': version}
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise RuntimeError('应用版本信息缺失或无效，请重新构建安装包。') from error

    environ = os.environ if environ is None else environ
    release_tag = environ.get('CODEFUEL_BUILD_TAG')
    git_root = _git(root, 'rev-parse', '--show-toplevel')
    has_repository = bool(git_root and Path(git_root).resolve() == root.resolve())
    if release_tag:
        version = normalize_version(release_tag)
        head = _git(root, 'rev-parse', 'HEAD') if has_repository else None
        tagged = _git(root, 'rev-parse', '--verify', f'refs/tags/{release_tag}^{{commit}}') if has_repository else None
        if not head or not tagged or head != tagged:
            raise RuntimeError('指定的构建 tag 不存在或未指向当前提交，请检出对应 tag 后构建。')
        return {'version': version, 'tag': release_tag, 'commit': head}

    tag = _git(root, 'describe', '--tags', '--abbrev=0', '--match', 'v[0-9]*', 'HEAD') if has_repository else None
    if tag:
        return {'version': normalize_version(tag), 'tag': tag, 'commit': _git(root, 'rev-parse', 'HEAD')}
    with (root / 'pyproject.toml').open('rb') as source:
        version = normalize_version(tomllib.load(source)['project']['version'])
    return {'version': version, 'tag': None, 'commit': _git(root, 'rev-parse', 'HEAD') if has_repository else None}


def write_build_info(root, output):
    info = get_version_info(root, frozen=False)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return info


if __name__ == '__main__':
    print(get_version_info()['version'])
