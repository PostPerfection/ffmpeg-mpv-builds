import ctypes
import sys
from pathlib import Path

EXPECTED_FFMPEG_LICENCE = "LGPL version 2.1 or later"
FORBIDDEN_FFMPEG_FLAGS = ["--enable-gpl", "--enable-nonfree", "--enable-version3"]
REQUIRED_MPV_FLAG = "-Dgpl=false"
LIBRARY_PATTERNS = {
    "linux": ("lib/libmpv.so.[0-9]", "lib/libavcodec.so.[0-9][0-9]"),
    "darwin": ("lib/libmpv.[0-9].dylib", "lib/libavcodec.[0-9][0-9].dylib"),
    "win32": ("bin/libmpv-[0-9].dll", "bin/avcodec-[0-9][0-9].dll"),
}


def find_one(archive, pattern):
    matches = list(archive.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(f"smoke test: expected one {pattern} in {archive}, found {matches}")
    return matches[0]


def check_ffmpeg(path):
    avcodec = ctypes.CDLL(str(path))
    avcodec.avcodec_license.restype = ctypes.c_char_p
    avcodec.avcodec_configuration.restype = ctypes.c_char_p
    licence = avcodec.avcodec_license().decode()
    configuration = avcodec.avcodec_configuration().decode()
    print(f"avcodec_license(): {licence}")
    print(f"avcodec_configuration(): {configuration}")
    assert licence == EXPECTED_FFMPEG_LICENCE, licence
    present = [flag for flag in FORBIDDEN_FFMPEG_FLAGS if flag in configuration.split()]
    assert not present, f"forbidden FFmpeg flags {present}"


def mpv_properties(path, names):
    mpv = ctypes.CDLL(str(path))
    mpv.mpv_create.restype = ctypes.c_void_p
    mpv.mpv_initialize.argtypes = [ctypes.c_void_p]
    mpv.mpv_set_option_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    mpv.mpv_get_property_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    mpv.mpv_get_property_string.restype = ctypes.c_void_p
    mpv.mpv_free.argtypes = [ctypes.c_void_p]
    mpv.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]
    handle = mpv.mpv_create()
    assert handle, "mpv_create returned NULL"
    mpv.mpv_set_option_string(handle, b"config", b"no")
    assert mpv.mpv_initialize(handle) == 0, "mpv_initialize failed"
    values = {}
    for name in names:
        pointer = mpv.mpv_get_property_string(handle, name.encode())
        values[name] = ctypes.string_at(pointer).decode() if pointer else ""
        mpv.mpv_free(pointer)
        print(f"{name}: {values[name]}")
    mpv.mpv_terminate_destroy(handle)
    return values


def main():
    archive, ffmpeg_tag, mpv_tag = Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]
    mpv_pattern, avcodec_pattern = LIBRARY_PATTERNS[sys.platform]
    avcodec_path = find_one(archive, avcodec_pattern)
    mpv_path = find_one(archive, mpv_pattern)
    print(f"libavcodec: {avcodec_path}")
    print(f"libmpv: {mpv_path}")
    check_ffmpeg(avcodec_path)
    values = mpv_properties(mpv_path, ["mpv-version", "mpv-configuration", "ffmpeg-version"])
    assert REQUIRED_MPV_FLAG in values["mpv-configuration"].split(), values["mpv-configuration"]
    assert values["mpv-version"] == f"mpv {mpv_tag}", values["mpv-version"]
    # libmpv reports the FFmpeg it loaded, so a system FFmpeg would show here
    assert values["ffmpeg-version"] == ffmpeg_tag, values["ffmpeg-version"]
    print("smoke test: passed")


main()
