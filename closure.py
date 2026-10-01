import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

BUILT_COMPONENT_PATTERNS = {
    "ffmpeg": re.compile(r"^(lib)?(av|sw)[a-z]+[-.]"),
    "mpv": re.compile(r"^libmpv[-.]"),
    "libplacebo": re.compile(r"^libplacebo[-.]"),
    "libdisplay-info": re.compile(r"^libdisplay-info[-.]"),
}
MACOS_SYSTEM_PREFIXES = ("/usr/lib/", "/System/Library/")
WINDOWS_API_SET_PREFIXES = ("api-ms-win-", "ext-ms-")
LICENCE_FILE_PATTERN = re.compile(r"^(COPYING|LICEN[CS]E|COPYRIGHT|NOTICE)", re.IGNORECASE)


def run(command):
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def staged_binaries(platform, stage):
    if platform == "windows":
        return sorted(path for path in (stage / "bin").glob("*.dll"))
    suffix_pattern = ".so" if platform == "linux" else ".dylib"
    return sorted(
        path for path in (stage / "lib").iterdir()
        if suffix_pattern in path.name and path.is_file() and not path.is_symlink()
    )


def staged_names(platform, stage):
    directory = stage / ("bin" if platform == "windows" else "lib")
    return {path.name.lower() if platform == "windows" else path.name for path in directory.iterdir()}


def macos_install_id(path):
    lines = run(["otool", "-D", str(path)]).splitlines()
    return lines[1].strip() if len(lines) > 1 else None


def macos_references(path):
    install_id = macos_install_id(path)
    references = []
    for line in run(["otool", "-L", str(path)]).splitlines()[1:]:
        reference = line.strip().split(" (compatibility")[0]
        if reference and reference != install_id:
            references.append(reference)
    return references


def macos_rpaths(path):
    lines = run(["otool", "-l", str(path)]).splitlines()
    return list(dict.fromkeys(
        lines[index + 2].strip().split()[1]
        for index, line in enumerate(lines)
        if line.strip() == "cmd LC_RPATH"
    ))


def macos_install_leaf(path):
    return Path(macos_install_id(path) or path.name).name


def is_macos_system(reference):
    return reference.startswith(MACOS_SYSTEM_PREFIXES)


def windows_imports(path):
    return [
        line.split("DLL Name:")[1].strip()
        for line in run(["objdump", "-p", str(path)]).splitlines()
        if "DLL Name:" in line
    ]


def is_windows_system(dll_name):
    if dll_name.lower().startswith(WINDOWS_API_SET_PREFIXES):
        return True
    return (Path(os.environ["SYSTEMROOT"]) / "System32" / dll_name).exists()


def resolve_macos_reference(reference, origin):
    if reference.startswith("@rpath/"):
        leaf = reference.removeprefix("@rpath/")
        for rpath in macos_rpaths(origin):
            candidate = Path(rpath.replace("@loader_path", str(origin.parent))) / leaf
            if candidate.exists():
                return candidate
        raise SystemExit(f"closure: {origin} loads {reference}, no LC_RPATH of it holds the file")
    if reference.startswith("@loader_path/"):
        return origin.parent / reference.removeprefix("@loader_path/")
    return Path(reference)


def bundle_macos(stage, origins):
    library_directory = stage / "lib"
    present = {path.name for path in library_directory.iterdir()}
    # references are read from the original file, whose LC_RPATH entries still point at its own prefix
    pending = staged_binaries("macos", stage)
    while pending:
        source_path = pending.pop()
        for reference in macos_references(source_path):
            leaf = Path(reference).name
            if is_macos_system(reference) or leaf in present:
                continue
            source = resolve_macos_reference(reference, source_path).resolve()
            shutil.copyfile(source, library_directory / leaf)
            os.chmod(library_directory / leaf, 0o755)
            present.add(leaf)
            origins[leaf] = str(source)
            pending.append(source)
    for path in staged_binaries("macos", stage):
        arguments = ["-id", f"@rpath/{macos_install_leaf(path)}"]
        for reference in macos_references(path):
            if not is_macos_system(reference):
                arguments += ["-change", reference, f"@loader_path/{Path(reference).name}"]
        for rpath in macos_rpaths(path):
            arguments += ["-delete_rpath", rpath]
        run(["install_name_tool", *arguments, str(path)])
        # every install_name_tool edit breaks the signature arm64 requires
        run(["codesign", "--force", "--sign", "-", str(path)])


def bundle_windows(stage, origins, package_prefix):
    binary_directory = stage / "bin"
    staged = {path.name.lower() for path in staged_binaries("windows", stage)}
    # import tables name dlls in any case, pacman only knows the file's own spelling
    package_dlls = {path.name.lower(): path for path in (package_prefix / "bin").glob("*.dll")}
    pending = sorted(staged)
    while pending:
        name = pending.pop()
        for dll_name in windows_imports(binary_directory / name):
            key = dll_name.lower()
            if key in staged:
                continue
            if key in package_dlls:
                source = package_dlls[key]
                shutil.copyfile(source, binary_directory / source.name)
                staged.add(key)
                origins[key] = str(source)
                pending.append(source.name)
            elif not is_windows_system(dll_name):
                raise SystemExit(f"closure: {name} imports {dll_name}, found neither in {package_prefix}/bin nor in Windows")


def read_origins(path):
    if not path or not path.exists():
        return {}
    return dict(line.split("\t", 1) for line in path.read_text(encoding="utf-8").splitlines() if line)


def write_origins(path, origins):
    path.write_text("".join(f"{name}\t{origin}\n" for name, origin in sorted(origins.items())), encoding="utf-8")


def read_allow_list(path):
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        soname, licence, reason = line.split("\t")
        rows[soname] = (licence, reason)
    return rows


def linux_closure(stage):
    environment = {key: value for key, value in os.environ.items() if key != "LD_LIBRARY_PATH"}
    closure = {}
    for binary in staged_binaries("linux", stage):
        output = subprocess.run(["ldd", str(binary)], check=True, capture_output=True, text=True, env=environment).stdout
        for line in output.splitlines():
            line = line.strip()
            if "=>" in line:
                soname, target = (part.strip() for part in line.split("=>", 1))
                if target.startswith("not found"):
                    raise SystemExit(f"closure: {binary.name} needs {soname}, which does not resolve")
                closure[soname] = target.split(" (")[0]
            elif line.startswith("/"):
                path = line.split(" (")[0]
                closure[Path(path).name] = path
    return closure


def dpkg_package(path):
    if not shutil.which("dpkg"):
        return "unknown"
    real_path = os.path.realpath(path)
    for candidate in (path, real_path, real_path.replace("/usr/lib/", "/lib/", 1)):
        result = subprocess.run(["dpkg", "-S", candidate], capture_output=True, text=True)
        owners = [line.rsplit(": ", 1)[0] for line in result.stdout.splitlines() if not line.startswith("diversion")]
        if result.returncode == 0 and owners:
            version = run(["dpkg-query", "-W", "-f", "${Version}", owners[0]])
            return f"{owners[0]} {version}"
    return "unknown"


# debian has no licence field to compare, the reviewed row is the record
def debian_package(origin, reviewed_licence):
    package = dpkg_package(origin)
    package_name = package.split()[0].split(":")[0]
    return f"Ubuntu {package}", reviewed_licence, [Path("/usr/share/doc") / package_name / "copyright"]


def brew_package(origin):
    parts = Path(origin).parts
    cellar_index = parts.index("Cellar")
    formula, version = parts[cellar_index + 1], parts[cellar_index + 2]
    info = json.loads(run(["brew", "info", "--json=v2", "--formula", formula]))["formulae"][0]
    keg = Path(*parts[: cellar_index + 3])
    licence_files = sorted(path for path in keg.iterdir() if path.is_file() and LICENCE_FILE_PATTERN.match(path.name))
    return f"Homebrew {formula} {version}", info["license"] or "", licence_files


def pacman_package(origin, package_prefix):
    msys_path = "/" + package_prefix.name + "/bin/" + Path(origin).name
    owner = run(["pacman", "-Qo", msys_path]).strip().split(" is owned by ")[1]
    package, version = owner.split()
    fields = dict(
        (key.strip(), value.strip())
        for key, value in (line.split(":", 1) for line in run(["pacman", "-Qi", package]).splitlines() if ":" in line)
    )
    msys_prefix = f"/{package_prefix.name}/"
    licence_files = [
        package_prefix / line.split(msys_prefix, 1)[1]
        for line in run(["pacman", "-Ql", package]).splitlines()
        if f"{msys_prefix}share/licenses/" in line and not line.endswith("/")
    ]
    return f"MSYS2 {package} {version}", fields["Licenses"], licence_files


def built_component(name):
    for component, pattern in BUILT_COMPONENT_PATTERNS.items():
        if pattern.match(name):
            return component
    return None


def check(arguments):
    stage = arguments.stage
    platform = arguments.platform
    allow_list = read_allow_list(arguments.allow_list)
    origins = read_origins(arguments.origins)
    package_prefix = Path(arguments.package_prefix) if arguments.package_prefix else None
    failures = []
    staged = staged_names(platform, stage)
    system_libraries = {}

    if platform == "linux":
        libraries = linux_closure(stage)
        for soname, path in sorted(libraries.items()):
            inside = Path(path).resolve().is_relative_to(stage.resolve())
            if soname in staged and not inside:
                failures.append(f"{soname} is in the archive but resolves to {path}")
            if not inside:
                system_libraries[soname] = dpkg_package(path)
        names = set(libraries) | {run(["patchelf", "--print-soname", str(binary)]).strip() for binary in staged_binaries(platform, stage)}
    elif platform == "macos":
        names = set()
        for binary in staged_binaries(platform, stage):
            for reference in macos_references(binary):
                if is_macos_system(reference):
                    system_libraries[reference] = "macOS"
                elif reference.startswith("@loader_path/") and Path(reference).name in staged:
                    names.add(Path(reference).name)
                else:
                    failures.append(f"{binary.name} loads {reference}, which is neither a macOS library nor in the archive")
            names.add(macos_install_leaf(binary))
    else:
        names = set()
        for binary in staged_binaries(platform, stage):
            names.add(binary.name.lower())
            for dll_name in windows_imports(binary):
                if dll_name.lower() in staged:
                    names.add(dll_name.lower())
                elif is_windows_system(dll_name):
                    system_libraries[dll_name] = "Windows"
                else:
                    failures.append(f"{binary.name} imports {dll_name}, which is neither a Windows library nor in the archive")

    allow_keys = {key.lower() if platform == "windows" else key: key for key in allow_list}
    bundled_packages = {}
    for name in sorted(names):
        row_key = allow_keys.get(name)
        if row_key is None:
            failures.append(f"{name} is in the closure but has no row in {arguments.allow_list.name}")
            continue
        allowed_licence = allow_list[row_key][0]
        if name in origins:
            if platform == "linux":
                package, package_licence, licence_files = debian_package(origins[name], allowed_licence)
            elif platform == "macos":
                package, package_licence, licence_files = brew_package(origins[name])
            else:
                package, package_licence, licence_files = pacman_package(origins[name], package_prefix)
            bundled_packages[name] = (package, package_licence, licence_files)
            if package_licence != allowed_licence:
                failures.append(f"{name} comes from {package} licensed '{package_licence}', the allow list reviewed '{allowed_licence}'")
        elif name in staged and built_component(name) is None:
            failures.append(f"{name} is in the archive but is neither built here nor a recorded package")

    unused_rows = sorted(set(allow_list) - {allow_keys[name] for name in names if name in allow_keys})

    for name in sorted(names):
        where = "archive" if name in staged else "system"
        source = bundled_packages[name][0] if name in bundled_packages else built_component(name) or system_libraries.get(name, "")
        print(f"{name}\t{where}\t{allow_list.get(allow_keys.get(name), ('NOT ALLOWED',))[0]}\t{source}")
    for name in sorted(set(system_libraries) - names):
        print(f"{name}\t{system_libraries[name]} system library")
    for row in unused_rows:
        print(f"note: allow list row {row} matches nothing in the closure")
    if failures:
        print("\n".join(f"FAIL: {failure}" for failure in failures), file=sys.stderr)
        raise SystemExit(1)
    print(f"closure: {len(names)} libraries, all in the allow list")

    write_third_party_licences(arguments, platform, stage, names, staged, allow_list, allow_keys, bundled_packages, system_libraries)


def licence_text(path):
    # some packages ship their licence in latin-1
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def write_third_party_licences(arguments, platform, stage, names, staged, allow_list, allow_keys, bundled_packages, system_libraries):
    components = dict(component.split("=", 1) for component in arguments.component)
    extra_licence_files = dict(item.split("=", 1) for item in arguments.licence_file)
    sections = ["Third-party libraries in this archive", "=" * 37, ""]
    for component, version in components.items():
        libraries = sorted(name for name in names if name in staged and built_component(name) == component)
        if not libraries:
            continue
        configure_line = (arguments.configure_lines / component).read_text(encoding="utf-8").strip()
        licences = sorted({allow_list[allow_keys[name]][0] for name in libraries})
        sections += [
            f"{component} {version}",
            f"  files: {' '.join(libraries)}",
            f"  licence: {' '.join(licences)}",
            f"  built here with: {configure_line}",
            "",
        ]
    for name, (package, package_licence, _) in sorted(bundled_packages.items()):
        sections += [name, f"  from: {package}", f"  licence: {package_licence}", "  built by the package's own recipe, copied with only its library search path changed", ""]
    if platform == "linux":
        sections += ["Loaded from the system, not shipped in this archive:"]
        sections += [f"  {name}  {system_libraries[name]}  {allow_list[allow_keys[name]][0]}" for name in sorted(system_libraries)]
        sections += [""]
    licence_texts = [("GNU Lesser General Public License version 2.1 (FFmpeg, mpv, libplacebo)", arguments.lgpl_text)]
    licence_texts += [(f"{name} licence", Path(path)) for name, path in extra_licence_files.items()]
    for name, (package, _, licence_files) in sorted(bundled_packages.items()):
        licence_texts += [(f"{name} ({package}): {path.name}", path) for path in licence_files]
    for title, path in licence_texts:
        sections += ["", title, "-" * len(title), licence_text(path)]
    (stage / "THIRD-PARTY-LICENSES").write_text("\n".join(sections), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    bundle_parser = commands.add_parser("bundle")
    bundle_parser.add_argument("--platform", choices=["macos", "windows"], required=True)
    bundle_parser.add_argument("--stage", type=Path, required=True)
    bundle_parser.add_argument("--origins", type=Path, required=True)
    bundle_parser.add_argument("--package-prefix")
    check_parser = commands.add_parser("check")
    check_parser.add_argument("--platform", choices=["linux", "macos", "windows"], required=True)
    check_parser.add_argument("--stage", type=Path, required=True)
    check_parser.add_argument("--allow-list", type=Path, required=True)
    check_parser.add_argument("--configure-lines", type=Path, required=True)
    check_parser.add_argument("--lgpl-text", type=Path, required=True)
    check_parser.add_argument("--origins", type=Path)
    check_parser.add_argument("--package-prefix")
    check_parser.add_argument("--component", action="append", default=[])
    check_parser.add_argument("--licence-file", action="append", default=[])
    arguments = parser.parse_args()

    if arguments.command == "check":
        check(arguments)
        return
    origins = {}
    if arguments.platform == "macos":
        bundle_macos(arguments.stage, origins)
    else:
        bundle_windows(arguments.stage, origins, Path(arguments.package_prefix))
    write_origins(arguments.origins, origins)
    print(f"closure: bundled {len(origins)} libraries: {' '.join(sorted(origins))}")


main()
