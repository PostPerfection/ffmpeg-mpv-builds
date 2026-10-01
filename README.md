# ffmpeg-mpv-builds

LGPL builds of FFmpeg n8.1.3 and libmpv v0.41.0 (with libplacebo v7.360.1) for Linux x86_64, macOS arm64 and Windows x86_64. postkit, guikit, dcpwizard and imfwizard load them in the same process as a closed-source GPU plugin, so nothing GPL may end up in an archive.

## Licence position

FFmpeg is configured without `--enable-gpl`, `--enable-version3` or `--enable-nonfree`, and with `--disable-autodetect`, so only the libraries named on the configure line join the link. mpv is configured with `-Dgpl=false -Dauto_features=disabled`, because `-Dgpl=false` alone still links GPL libraries it finds (rubberband did). CI fails when any library in the link closure has no row in `allow-list/<platform>.tsv`, and on macOS and Windows when the package manager's licence field differs from the reviewed row. The smoke test asserts `avcodec_license()` is `LGPL version 2.1 or later` and that libmpv reports `-Dgpl=false`.

## Use in CI

```yaml
- uses: PostPerfection/ffmpeg-mpv-builds@v1
  with:
    release: v1.0.0
```

The action checks the archive against the release's `SHA256SUMS` and exports:

- Linux: `PKG_CONFIG_PATH`, `LD_LIBRARY_PATH`, and installs the Ubuntu packages in `ubuntu-24.04-runtime-packages.txt`.
- macOS: `PKG_CONFIG_PATH`, `DYLD_LIBRARY_PATH`.
- Windows: `MPV_LIB_DIR` (holds `mpv.lib`), `FFMPEG_DIR` (for ffmpeg-sys-next), and `bin` on `PATH`.
- All: `FFMPEG_MPV_DIR` and the `dir` output, the archive root, for installers to copy from.

## Rebuild locally

`./build.sh` runs every step, `./build.sh <step>...` runs some, in this order: `deps fetch libdisplay_info libplacebo ffmpeg_configure ffmpeg_build mpv_configure mpv_build stage licences smoke package`, then `sources` for the source asset. Output lands in `build/dist`, or `$WORK/dist` when `WORK` is set.

Linux, in the same image CI uses:

```sh
podman run --rm -v "$PWD":/repo:z -w /repo ubuntu:24.04 ./build.sh
```

macOS needs Homebrew and Python 3. Windows needs an MSYS2 UCRT64 shell and Visual Studio with the C++ tools, for `lib.exe`.

## Notes

- The Linux archive is built on Ubuntu 24.04 and needs glibc 2.38 or newer.
- On Linux the archive carries FFmpeg, mpv, libplacebo, libdisplay-info and LuaJIT, with `$ORIGIN` rpaths. libplacebo and libdisplay-info change soname between distros, and LuaJIT is not installed by default everywhere. Everything else comes from the system and has a stable soname.
- mpv is built with LuaJIT because the apps set `osc=no`, and mpv only knows that option with Lua.
- On macOS the dylibs have `@rpath/` install names and reference each other through `@loader_path/`. An app bundle needs an `LC_RPATH` to the directory it puts them in.
- Windows uses UCRT64 so the DLLs share the UCRT C runtime with MSVC-built Rust binaries. `.lib` import libraries are made with `gendef` and `lib.exe`.
- macOS and Windows libraries the OS ships (`/usr/lib`, `/System/Library`, `System32`, API sets) need no allow-list row.
- `.pc` files use `prefix=${pcfiledir}/../..` and have no `Requires.private` or `Libs.private`, so they work wherever the archive is unpacked without the system's -dev packages.
