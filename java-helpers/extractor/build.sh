#!/bin/sh
# Builds build/dsarp-extractor.jar with the JDK only (no Maven, no downloads).
# Needs JDK 17 or newer.
set -eu
cd "$(dirname "$0")"
rm -rf build/classes
mkdir -p build/classes
javac --release 17 -encoding UTF-8 -Xlint:all -Werror -d build/classes $(find src/main/java -name '*.java')
jar --create --file build/dsarp-extractor.jar --main-class dsarp.extractor.Main -C build/classes .
echo "built $(pwd)/build/dsarp-extractor.jar"
