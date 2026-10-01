#!/usr/bin/env bash
# usage: build.sh [step...], steps: deps fetch libdisplay_info libplacebo ffmpeg_configure ffmpeg_build mpv_configure mpv_build stage licences smoke package sources, default runs deps to package
set -euo pipefail

FFMPEG_TAG=n8.1.3
FFMPEG_COMMIT=1041abdc962f4cc4f394aa8de9dc5236c0c3b9e7
MPV_TAG=v0.41.0
MPV_COMMIT=41f6a645068483470267271e1d09966ca3b9f413
LIBPLACEBO_TAG=v7.360.1
LIBPLACEBO_COMMIT=cee9b076f2c63104ccfd497fa79c39a867293ec4
LIBDISPLAY_INFO_TAG=0.3.0
LIBDISPLAY_INFO_COMMIT=47a5590e9c4eb35d67651b8c05a55f1a48259329
MESON_VERSION=1.10.2

FFMPEG_URL=https://git.ffmpeg.org/ffmpeg.git
MPV_URL=https://github.com/mpv-player/mpv.git
LIBPLACEBO_URL=https://code.videolan.org/videolan/libplacebo.git
LIBDISPLAY_INFO_URL=https://gitlab.freedesktop.org/emersion/libdisplay-info.git
# demos/3rdparty/nuklear is only for the demos
LIBPLACEBO_SUBMODULES=(3rdparty/glad 3rdparty/jinja 3rdparty/markupsafe 3rdparty/Vulkan-Headers 3rdparty/fast_float)

FFMPEG_LICENSE_LINE='License: LGPL version 2.1 or later'
FFMPEG_LIBRARIES=(avcodec avformat avutil avfilter avdevice swscale swresample)
UBUNTU_RUNTIME_PACKAGES=ubuntu-24.04-runtime-packages.txt
# stable soname, but fedora and others do not install it by default
LINUX_COPIED_SYSTEM_LIBRARIES=(libluajit-5.1.so.2)

ROOT="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORK:-$ROOT/build}"
SOURCES="$WORK/src"
PREFIX="$WORK/prefix"
CONFIGURE_LINES="$WORK/configure"
BUNDLE_ORIGINS="$WORK/bundled-origins.tsv"
DIST="$WORK/dist"
VENV="$WORK/venv"
JOBS="${JOBS:-$(getconf _NPROCESSORS_ONLN)}"

case "$(uname -s)" in
    Linux) PLATFORM=linux ARCHIVE_NAME=ffmpeg-mpv-linux-x86_64 ;;
    Darwin) PLATFORM=macos ARCHIVE_NAME=ffmpeg-mpv-macos-arm64 ;;
    MINGW64_NT* | MSYS_NT*)
        PLATFORM=windows ARCHIVE_NAME=ffmpeg-mpv-windows-x86_64
        if [ "${MSYSTEM:-}" != UCRT64 ]; then
            echo "build.sh: run this from an MSYS2 UCRT64 shell, MSYSTEM is '${MSYSTEM:-}'" >&2
            exit 1
        fi
        ;;
    *) echo "build.sh: unsupported system $(uname -s)" >&2; exit 1 ;;
esac
STAGE="$WORK/stage/$ARCHIVE_NAME"

export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"

venv_bin() {
    if [ -d "$VENV/Scripts" ]; then echo "$VENV/Scripts"; else echo "$VENV/bin"; fi
}

meson() {
    "$(venv_bin)/meson" "$@"
}

python() {
    "$(venv_bin)/python" "$@"
}

record_configure_line() {
    local name="$1"
    shift
    mkdir -p "$CONFIGURE_LINES"
    printf '%s\n' "$*" > "$CONFIGURE_LINES/$name"
}

deps() {
    case "$PLATFORM" in
        linux)
            export DEBIAN_FRONTEND=noninteractive
            apt-get update
            apt-get install -y --no-install-recommends \
                build-essential ca-certificates git nasm pkgconf ninja-build patchelf \
                python3 python3-venv xz-utils file hwdata \
                libdav1d-dev libzimg-dev libass-dev liblcms2-dev zlib1g-dev libluajit-5.1-dev \
                libva-dev libdrm-dev libffmpeg-nvenc-dev libegl-dev \
                libwayland-dev wayland-protocols libxkbcommon-dev \
                libpulse-dev libpipewire-0.3-dev libasound2-dev
            ;;
        macos)
            brew install --quiet ninja pkgconf dav1d zimg libass little-cms2 luajit
            ;;
        windows)
            pacman -S --noconfirm --needed git make diffutils zip \
                mingw-w64-ucrt-x86_64-{gcc,nasm,ninja,pkgconf,python,tools,binutils} \
                mingw-w64-ucrt-x86_64-{dav1d,zimg,libass,lcms2,zlib,luajit,ffnvcodec-headers}
            ;;
    esac
    [ -x "$(venv_bin)/meson" ] || {
        python3 -m venv "$VENV"
        python -m pip install --quiet "meson==$MESON_VERSION"
    }
    meson --version
}

clone_pinned() {
    local url="$1" tag="$2" commit="$3" directory="$4"
    if [ ! -d "$directory" ]; then
        git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$tag" "$url" "$directory"
    fi
    local head
    head="$(git -C "$directory" rev-parse HEAD)"
    if [ "$head" != "$commit" ]; then
        echo "build.sh: $directory is at $head, the pin for $tag is $commit" >&2
        exit 1
    fi
}

fetch() {
    mkdir -p "$SOURCES"
    clone_pinned "$FFMPEG_URL" "$FFMPEG_TAG" "$FFMPEG_COMMIT" "$SOURCES/ffmpeg"
    clone_pinned "$MPV_URL" "$MPV_TAG" "$MPV_COMMIT" "$SOURCES/mpv"
    clone_pinned "$LIBPLACEBO_URL" "$LIBPLACEBO_TAG" "$LIBPLACEBO_COMMIT" "$SOURCES/libplacebo"
    git -C "$SOURCES/libplacebo" submodule update --init --depth 1 "${LIBPLACEBO_SUBMODULES[@]}"
    if [ "$PLATFORM" = linux ]; then
        clone_pinned "$LIBDISPLAY_INFO_URL" "$LIBDISPLAY_INFO_TAG" "$LIBDISPLAY_INFO_COMMIT" "$SOURCES/libdisplay-info"
    fi
}

meson_setup() {
    local name="$1" source="$2"
    shift 2
    local arguments=(--prefix="$PREFIX" --libdir=lib --buildtype=release -Dauto_features=disabled "$@")
    record_configure_line "$name" meson setup "${arguments[@]}"
    rm -rf "$WORK/build-$name"
    meson setup "$WORK/build-$name" "$source" "${arguments[@]}"
}

meson_build() {
    meson compile -C "$WORK/build-$1" -j "$JOBS"
    meson install -C "$WORK/build-$1" --strip
}

# mpv's drm support needs it, and its soname differs between distros
libdisplay_info() {
    [ "$PLATFORM" = linux ] || return 0
    meson_setup libdisplay-info "$SOURCES/libdisplay-info"
    meson_build libdisplay-info
}

libplacebo() {
    meson_setup libplacebo "$SOURCES/libplacebo" \
        -Dopengl=enabled -Dlcms=enabled -Ddovi=enabled \
        -Ddemos=false -Dtests=false -Dbench=false -Dfuzz=false
    meson_build libplacebo
}

ffmpeg_platform_flags() {
    case "$PLATFORM" in
        linux) echo --enable-pthreads --enable-iconv --enable-ffnvcodec --enable-cuvid --enable-nvdec --enable-vaapi --enable-libdrm ;;
        macos) echo --enable-pthreads --enable-iconv --enable-videotoolbox ;;
        windows) echo --enable-w32threads --enable-ffnvcodec --enable-nvdec --enable-d3d11va ;;
    esac
}

# --disable-autodetect so nothing on the build machine joins the link unasked
ffmpeg_configure() {
    local arguments=(
        --prefix="$PREFIX" --libdir="$PREFIX/lib"
        --enable-shared --disable-static
        --disable-programs --disable-doc
        --disable-autodetect --enable-zlib
        --enable-libdav1d --enable-libzimg
    )
    # shellcheck disable=SC2207
    arguments+=($(ffmpeg_platform_flags))
    record_configure_line ffmpeg configure "${arguments[@]}"
    rm -rf "$WORK/build-ffmpeg"
    mkdir -p "$WORK/build-ffmpeg"
    cd "$WORK/build-ffmpeg"
    "$SOURCES/ffmpeg/configure" "${arguments[@]}" | tee configure.out
    grep -qx "$FFMPEG_LICENSE_LINE" configure.out
}

ffmpeg_build() {
    cd "$WORK/build-ffmpeg"
    make -j"$JOBS"
    make install
}

mpv_platform_flags() {
    case "$PLATFORM" in
        linux)
            echo -Diconv=enabled -Degl=enabled -Ddrm=enabled -Dwayland=enabled \
                -Dvaapi=enabled -Dvaapi-drm=enabled -Dvaapi-wayland=enabled \
                -Dcuda-hwaccel=enabled -Dcuda-interop=enabled \
                -Dalsa=enabled -Dpulse=enabled -Dpipewire=enabled
            ;;
        macos)
            echo -Diconv=enabled -Dcocoa=enabled -Dgl-cocoa=enabled \
                -Dvideotoolbox-gl=enabled -Dcoreaudio=enabled
            ;;
        windows)
            echo -Dwin32-threads=enabled -Dgl-win32=enabled -Dd3d-hwaccel=enabled \
                -Dcuda-hwaccel=enabled -Dcuda-interop=enabled -Dwasapi=enabled
            ;;
    esac
}

# -Dgpl=false alone still links GPL libraries it finds, so every feature is
# off unless listed here. lua is on because the apps set osc=no, which mpv
# only knows with lua
mpv_configure() {
    # shellcheck disable=SC2046
    meson_setup mpv "$SOURCES/mpv" \
        -Dgpl=false -Dlibmpv=true -Dcplayer=false -Dtests=false -Dfuzzers=false \
        -Dbuild-date=false -Dlua=luajit \
        -Dgl=enabled -Dplain-gl=enabled -Dvector=enabled \
        -Dlcms2=enabled -Dzimg=enabled -Dzlib=enabled \
        $(mpv_platform_flags)
}

mpv_build() {
    meson_build mpv
}

rewrite_pkgconfig() {
    local pc_file="$1" prefix_forms=("$PREFIX")
    [ "$PLATFORM" != windows ] || prefix_forms+=("$(cygpath -m "$PREFIX")")
    for form in "${prefix_forms[@]}"; do
        sed -i.orig -e "s|$form|\${prefix}|g" "$pc_file"
    done
    sed -i.orig \
        -e 's|^prefix=.*|prefix=${pcfiledir}/../..|' \
        -e '/^Requires.private:/d' -e '/^Libs.private:/d' \
        "$pc_file"
    rm -f "$pc_file.orig"
}

# the setup action installs these on ubuntu runners so the system half of the closure is present
ubuntu_runtime_packages() {
    find "$STAGE/lib" -maxdepth 1 -type f -name '*.so*' -exec readelf -d {} + \
        | awk '/NEEDED/ {gsub(/[][]/, "", $5); print $5}' | sort -u \
        | while read -r soname; do
            [ ! -e "$STAGE/lib/$soname" ] || continue
            dpkg -S "$(readlink -f "$(ldconfig -p | awk -v name="$soname" '$1 == name {print $NF; exit}')")" \
                | grep -v '^diversion' | head -1 | cut -d: -f1
        done | sort -u
}

msvc_lib_exe() {
    local vs_root msvc_tools
    vs_root="$("/c/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe" -latest -products '*' \
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath | tr -d '\r')"
    msvc_tools="$(ls -d "$(cygpath -u "$vs_root")/VC/Tools/MSVC"/* | sort -V | tail -1)"
    echo "$msvc_tools/bin/Hostx64/x64/lib.exe"
}

# rust's msvc toolchain links against .lib import libraries, mingw only makes .dll.a
windows_import_libraries() {
    local lib_exe
    lib_exe="$(msvc_lib_exe)"
    cd "$STAGE/lib"
    for dll in "$STAGE"/bin/av*.dll "$STAGE"/bin/sw*.dll "$STAGE"/bin/libmpv-*.dll; do
        local dll_name library
        dll_name="$(basename "$dll" .dll)"
        library="${dll_name%-*}"
        library="${library#lib}"
        gendef - "$dll" > "$dll_name.def"
        # dash flags because msys rewrites /nologo into a path
        "$lib_exe" -nologo -def:"$dll_name.def" -machine:x64 -out:"$library.lib"
        rm "$dll_name.def" "$library.exp"
    done
    cd "$ROOT"
}

stage() {
    rm -rf "$STAGE"
    mkdir -p "$STAGE/lib/pkgconfig" "$STAGE/include"
    for header_dir in "${FFMPEG_LIBRARIES[@]/#/lib}" mpv; do
        cp -R "$PREFIX/include/$header_dir" "$STAGE/include/"
    done
    for pc_name in "${FFMPEG_LIBRARIES[@]/#/lib}" mpv; do
        cp "$PREFIX/lib/pkgconfig/$pc_name.pc" "$STAGE/lib/pkgconfig/"
        rewrite_pkgconfig "$STAGE/lib/pkgconfig/$pc_name.pc"
    done
    case "$PLATFORM" in
        linux)
            for library in "${FFMPEG_LIBRARIES[@]}" mpv placebo display-info; do
                cp -P "$PREFIX/lib/lib$library.so"* "$STAGE/lib/"
            done
            : > "$BUNDLE_ORIGINS"
            for soname in "${LINUX_COPIED_SYSTEM_LIBRARIES[@]}"; do
                origin="$(readlink -f "$(ldconfig -p | awk -v name="$soname" '$1 == name {print $NF; exit}')")"
                cp "$origin" "$STAGE/lib/$soname"
                printf '%s\t%s\n' "$soname" "$origin" >> "$BUNDLE_ORIGINS"
            done
            find "$STAGE/lib" -maxdepth 1 -type f -name '*.so*' -exec patchelf --set-rpath '$ORIGIN' {} \;
            ubuntu_runtime_packages > "$STAGE/$UBUNTU_RUNTIME_PACKAGES"
            ;;
        macos)
            for library in "${FFMPEG_LIBRARIES[@]}" mpv placebo; do
                cp -P "$PREFIX/lib/lib$library".*dylib "$STAGE/lib/"
            done
            python3 "$ROOT/closure.py" bundle --platform macos --stage "$STAGE" --origins "$BUNDLE_ORIGINS"
            ;;
        windows)
            mkdir -p "$STAGE/bin"
            cp "$PREFIX"/bin/*.dll "$STAGE/bin/"
            cp "$PREFIX"/lib/*.dll.a "$STAGE/lib/"
            python "$ROOT/closure.py" bundle --platform windows --stage "$STAGE" --origins "$BUNDLE_ORIGINS" \
                --package-prefix "$(cygpath -m "$MINGW_PREFIX")"
            windows_import_libraries
            ;;
    esac
}

licence_python() {
    if [ "$PLATFORM" = windows ]; then python "$@"; else python3 "$@"; fi
}

licences() {
    local arguments=(
        --platform "$PLATFORM" --stage "$STAGE"
        --allow-list "$ROOT/allow-list/$PLATFORM.tsv"
        --configure-lines "$CONFIGURE_LINES"
        --lgpl-text "$SOURCES/ffmpeg/COPYING.LGPLv2.1"
        --component "ffmpeg=$FFMPEG_TAG" --component "mpv=$MPV_TAG"
        --component "libplacebo=$LIBPLACEBO_TAG" --component "libdisplay-info=$LIBDISPLAY_INFO_TAG"
    )
    arguments+=(--origins "$BUNDLE_ORIGINS")
    [ "$PLATFORM" != linux ] || arguments+=(--licence-file "libdisplay-info=$SOURCES/libdisplay-info/LICENSE")
    [ "$PLATFORM" != windows ] || arguments+=(--package-prefix "$(cygpath -m "$MINGW_PREFIX")")
    licence_python "$ROOT/closure.py" check "${arguments[@]}"
}

smoke() {
    licence_python "$ROOT/smoke_test.py" "$STAGE" "$FFMPEG_TAG" "$MPV_TAG"
}

package() {
    mkdir -p "$DIST"
    cp "$STAGE/THIRD-PARTY-LICENSES" "$DIST/THIRD-PARTY-LICENSES-$PLATFORM.txt"
    cd "$WORK/stage"
    if [ "$PLATFORM" = windows ]; then
        rm -f "$DIST/$ARCHIVE_NAME.zip"
        zip -qr -X "$DIST/$ARCHIVE_NAME.zip" "$ARCHIVE_NAME"
    else
        tar -cJf "$DIST/$ARCHIVE_NAME.tar.xz" "$ARCHIVE_NAME"
    fi
    cd "$ROOT"
    ls -l "$DIST"
}

source_tarball() {
    local name="$1" directory="$2"
    tar -C "$(dirname "$directory")" --exclude=.git --sort=name --mtime=@0 \
        --owner=0 --group=0 --numeric-owner \
        --transform "s|^$(basename "$directory")|$name|" \
        -cJf "$WORK/sources/$name.tar.xz" "$(basename "$directory")"
}

# one asset with every source tree the archives are built from, plus this recipe
sources() {
    rm -rf "$WORK/sources"
    mkdir -p "$WORK/sources" "$DIST"
    source_tarball "ffmpeg-$FFMPEG_TAG" "$SOURCES/ffmpeg"
    source_tarball "mpv-$MPV_TAG" "$SOURCES/mpv"
    source_tarball "libplacebo-$LIBPLACEBO_TAG" "$SOURCES/libplacebo"
    source_tarball "libdisplay-info-$LIBDISPLAY_INFO_TAG" "$SOURCES/libdisplay-info"
    git -C "$ROOT" ls-files -co --exclude-standard -z \
        | tar -C "$ROOT" --null -T - --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
            --transform 's|^|ffmpeg-mpv-builds/|' -cJf "$WORK/sources/ffmpeg-mpv-builds-recipe.tar.xz"
    tar -C "$WORK" --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
        -cf "$DIST/ffmpeg-mpv-sources.tar" sources
    ls -l "$WORK/sources" "$DIST/ffmpeg-mpv-sources.tar"
}

steps=("$@")
[ ${#steps[@]} -gt 0 ] || steps=(deps fetch libdisplay_info libplacebo ffmpeg_configure ffmpeg_build mpv_configure mpv_build stage licences smoke package)
for step in "${steps[@]}"; do
    "$step"
done
