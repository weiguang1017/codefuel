import json
import subprocess

import pytest

from backend.version import get_version_info, normalize_version, write_build_info


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'source';root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    git(root, 'config', 'user.name', 'Fixture')
    (root / 'pyproject.toml').write_text('[project]\nversion="0.1.0"\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'fixture')
    git(root, 'tag', 'v2.4.0')
    return root


def test_version_follows_reachable_tag_instead_of_project_constant(repo):
    info = get_version_info(repo, frozen=False, environ={})
    assert info['version'] == '2.4.0' and info['tag'] == 'v2.4.0'
    (repo / 'change.txt').write_text('fixture')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'later source')
    assert get_version_info(repo, frozen=False, environ={})['version'] == '2.4.0'
    git(repo, 'tag', 'v2.5.0-rc.1')
    assert get_version_info(repo, frozen=False, environ={})['version'] == '2.5.0-rc.1'


def test_explicit_release_tag_must_point_to_checked_out_commit(repo):
    info = get_version_info(repo, frozen=False, environ={'CODEFUEL_BUILD_TAG': 'v2.4.0'})
    assert info['version'] == '2.4.0'
    with pytest.raises(RuntimeError, match='tag'):
        get_version_info(repo, frozen=False, environ={'CODEFUEL_BUILD_TAG': 'v9.9.9'})
    git(repo, 'commit', '--allow-empty', '-qm', 'not the tagged source')
    with pytest.raises(RuntimeError, match='tag'):
        get_version_info(repo, frozen=False, environ={'CODEFUEL_BUILD_TAG': 'v2.4.0'})


def test_packaged_version_is_fixed_even_with_different_environment_and_working_folder(repo, tmp_path, monkeypatch):
    monkeypatch.delenv('CODEFUEL_BUILD_TAG', raising=False)
    bundle = tmp_path / 'bundle';bundle.mkdir()
    info = write_build_info(repo, bundle / 'build-info.json')
    git(repo, 'tag', '-a', 'v9.0.0', '-m', 'different release')
    monkeypatch.chdir(repo)
    actual = get_version_info(bundle, frozen=True, environ={'CODEFUEL_BUILD_TAG': 'v9.0.0'})
    assert actual == info and actual['version'] == '2.4.0'


@pytest.mark.parametrize('payload', [None, {}, {'version': 'garbage'}, {'version': '2.4.0', 'tag': 'v9.0.0'}])
def test_unstamped_or_inconsistent_bundle_fails_explicitly(tmp_path, payload):
    if payload is not None:
        (tmp_path / 'build-info.json').write_text(json.dumps(payload))
    with pytest.raises(RuntimeError, match='版本信息'):
        get_version_info(tmp_path, frozen=True)


def test_source_archive_uses_project_version_and_ignores_parent_repository(repo):
    archive = repo / 'archive';archive.mkdir()
    (archive / 'pyproject.toml').write_text('[project]\nversion="1.6.3"\n')
    assert get_version_info(archive, frozen=False, environ={})['version'] == '1.6.3'
    assert get_version_info(archive, frozen=False, environ={})['tag'] is None


@pytest.mark.parametrize('value', ['v1.2', 'v1.2.3/unsafe', 'release-1.2.3', '', 'v01.2.3'])
def test_invalid_version_tag_is_rejected(value):
    with pytest.raises(ValueError): normalize_version(value)
