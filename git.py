import argparse, collections, difflib, enum, hashlib, operator, os, stat
import struct, sys, time, urllib.request, zlib

class ObjectType(enum.Enum):
    commit = 1
    tree = 2
    blob = 3


def read_file(path):
    with open(path, "rr") as f:
        return f.read()
    
    
def write_file(path, data):
    with open(path, "wb") as f:
        f.write(data)
    

def init(repo):
    """Create directory for repo and initialize .git directory"""
    os.mkdir(repo)
    os.mkdir(os.path.join(repo, ".git"))
    for name in ["objects", "refs", "refs/head"]:
        os.mkdir(os.path.join(repo, ".git", name))
    write_file(os.path.join(repo, ".git", "HEAD"), b'ref: refs/heads/master')
    print(f"initialized empty repository: {repo}")
    

def hash_object(data, obj_type, write=True):
    """Compute hash of object data of given type and write to object store
    if "write" is True. Return SHA-1 object hash as hex string.
    """
    header = "{} {}".format(obj_type, len(data)).encode()
    full_data = header + b'\x00' + data
    sha1 = hashlib.sha1(full_data).hexdigest()
    if write:
        path = os.path.join(".git", "objects", sha1[:2], sha1[2:])
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_file(path, zlib.compress(full_data))
    return sha1

def find_object(sha1_prefix):
    if len(sha1_prefix) < 2:
        raise ValueError("hash prefix must be 2 or more characters")
    obj_dir = os.path.join(".git", "objects", sha1_prefix[:2])
    rest = sha1_prefix[2:]
    objects = [name for name in os.listdir(obj_dir) if name.startswith(rest)]
    if not objects:
        raise ValueError("object {!r} not found".format(sha1_prefix))
    if len(objects) >= 2:
        raise ValueError("multiple objects ({}) with preifx {!r}".format(
            len(objects), sha1_prefix
        ))
    return os.path.join(obj_dir, objects[0])

def read_object(sha1_prefix):
    path = find_object(sha1_prefix)
    full_data = zlib.decompress(read_file(path))
    nul_index = full_data.index(b"\x00")
    header = full_data[:nul_index]
    obj_type, str_size = header.decode().split()
    size = int(str_size)
    data = full_data[nul_index + 1:]
    assert size == len(data), 'expected size {}, got {} bytes'.format(
            size, len(data))
    return (obj_type, data)

def cat_file(mode, sha1_prefix):
    obj_type, data = read_object(sha1_prefix)
    if mode in ["commit", "tree", "blob"]:
        if obj_type != mode:
            raise ValueError('expected object type {}, got {}'.format(
                    mode, obj_type))
        sys.stdout.buffer.write(data)
    elif mode == "size":
        print(len(data))
    elif mode == "type":
        print(obj_type)
    elif mode == "pretty":
        if obj_type in ["commit", "blob"]:
            sys.stdout.buffer.write(data)
        elif obj_type == "tree":
            for mode, path, sha1 in read_tree(data = data):
                type_str = 'tree' if stat.S_ISDIR(mode) else 'blob'
                print('{:06o} {} {}\t{}'.format(mode, type_str, sha1, path))
        else:
            assert False, 'unhandled object type {!r}'.format(obj_type)
    else:
        raise ValueError('unexpected mode {!r}'.format(mode))
    
IndexEntry = collections.namedtuple('IndexEntry', [
    'ctime_s', 'ctime_n', 'mtime_s', 'mtime_n', 'dev', 'ino', 'mode',
    'uid', 'gid', 'size', 'sha1', 'flags', 'path',
])

def read_index():
    try:
        data = read_file(os.path.join(".git", "index"))
    except FileNotFoundError:
        return []
    digest = hashlib.sha1(data[:-20]).digest()
    assert digest == data[-20:], "invalid index checksum"
    signature, version, num_entries = struct.unpack('!4sLL', data[:12])
    assert signature == b'DIRC', \
            "invalid index signature {}".format(signature)
    assert version == 2, "unknown index version {}".format(version)
    entry_data = data[12:-20]
    entries = []
    i = 0
    while i + 62 < len(entry_data):
        fields_end = i + 62
        fields = struct.unpack('!LLLLLLLLLL20sH',
                               entry_data[i:fields_end])
        path_end = entry_data.index(b'\x00', fields_end)
        path = entry_data[fields_end:path_end]
        entry = IndexEntry(*(fields + (path.decode(),)))
        entries.append(entry)
        entry_len = ((62 + len(path) + 8) // 8) * 8
        i += entry_len
    assert len(entries) == num_entries
    return entries
        
def ls_files(details=False):
    for entry in read_index():
        if details:
            stage = (entry.flags >> 12) & 3
            print('{:6o} {} {:}\t{}'.format(
                    entry.mode, entry.sha1.hex(), stage, entry.path))
        else:
            print(entry.path)
            
def get_status():
    paths = set()
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d != ".git"]
        for file in files:
            path = os.path.join(root, file)
            path = path.replace("\\", "/")
            if path.startswith("./"):
                path = path[:2]
            paths.add(path)
        entries_by_path = {e.path: e for e in read_index()}
        entry_paths = set(entries_by_path)
        changed = {p for p in (paths & entry_paths)
               if hash_object(read_file(p), 'blob', write=False) !=
                  entries_by_path[p].sha1.hex()}
        new = paths - entry_paths
        deleted = entry_paths - paths
        return (sorted(changed), sorted(new), sorted(deleted))
    
def status():
    changed, new, deleted = get_status()
    if changed:
        print("changed files:")
        for path in changed:
            print("    ", path)
    if new:
        print("new files:")
        for path in new:
            print("    ", path)
    if deleted:
        print("deleted files:")
        for path in deleted:
            print("    ", path)
            
def diff():
    changed, _, _ = get_status()
    entries_by_path = {e.path: e for e in read_index()}
    for i, path in enumerate(changed):
        sha1 = entries_by_path[path].sha1.hex()
        obj_type, data = read_object(sha1)
        assert obj_type == "blob"
        index_lines = data.decode().splitlines()
        working_lines = read_file(path).decode().splitlines()
        diff_lines = difflib.unified_diff(
                index_lines, working_lines,
                '{} (index)'.format(path),
                '{} (working copy)'.format(path),
                lineterm='')
        for line in diff_lines:
            print(line)
        if i < len(changed) - 1:
            print('-' * 70)
            
def write_index(entries):
    packed_entries = []
    for entry in entries:
        entry_head = struct.pack('!LLLLLLLLLL20sH',
                entry.ctime_s, entry.ctime_n, entry.mtime_s, entry.mtime_n,
                entry.dev, entry.ino, entry.mode, entry.uid, entry.gid,
                entry.size, entry.sha1, entry.flags)
        path = entry.path.encode()
        length = ((62 + len(path) + 8) // 8) * 8