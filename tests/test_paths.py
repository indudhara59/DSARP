import pytest

from dsarp import paths


@pytest.mark.parametrize("path,package,module", [
    ("tika-core/src/main/java/org/apache/tika/Tika.java", "org.apache.tika", "tika-core"),
    ("core/src/test/java/PackagelessAction.java", "", "core"),
    ("src/main/java/a/B.java", "a", "."),
    ("src/java/org/apache/cassandra/db/Mutation.java", "org.apache.cassandra.db", "."),
    ("tools/stress/src/org/apache/cassandra/stress/Stress.java", "org.apache.cassandra.stress", "tools/stress"),
    ("tools/fqltool/test/unit/org/apache/cassandra/fqltool/T.java", "org.apache.cassandra.fqltool", "tools/fqltool"),
    ("jaas/boot/src/main/java/org/apache/karaf/jaas/boot/A.java", "org.apache.karaf.jaas.boot", "jaas/boot"),
])
def test_module_of_any_layout(path, package, module):
    assert paths.module_of(path, package) == module


@pytest.mark.parametrize("path,package,expected", [
    ("core/src/test/java/a/BTest.java", "a", True),
    ("core/src/main/java/a/B.java", "a", False),
    ("tools/fqltool/test/unit/x/T.java", "x", True),
    ("src/java/x/Y.java", "x", False),
    ("mod/src/it/java/x/IT.java", "x", True),
])
def test_is_test_path(path, package, expected):
    assert paths.is_test_path(path, package) is expected


def test_package_mismatch_falls_back_to_maven_layout():
    # Package does not match the folders: the Maven src/<set>/java root is used.
    assert paths.module_of("m/src/main/java/x/Y.java", None) == "m"
    assert paths.source_root("m/weird/x/Y.java", "a.b") is None


def test_generated_and_test_modules():
    assert paths.is_generated_path("log4j-perf-test/target/generated-sources/annotations/a/B.java")
    assert not paths.is_generated_path("core/src/main/java/a/B.java")
    assert paths.is_test_module("log4j-core-test")
    assert paths.is_test_module("itests/test")
    assert paths.is_test_module("tika-integration-tests/tika-woodstox-tests")
    assert not paths.is_test_module("tika-core")
    assert not paths.is_test_module("contest-engine")


def test_normalize_collapses_dot_segments():
    assert paths.normalize("/Users/x/karaf/./jms/src/A.java") == "/Users/x/karaf/jms/src/A.java"
    assert paths.normalize("./a/b/") == "a/b"
    assert paths.normalize("") == ""


def test_infer_root_across_per_module_runs():
    files = [
        ("/Users/x/karaf/./jaas/boot/src/main/java/o/a/A.java", "o.a"),
        ("/Users/x/karaf/./jaas/blueprint/jasypt/src/main/java/o/b/B.java", "o.b"),
        ("/Users/x/karaf/main/src/test/java/o/c/C.java", "o.c"),
    ]
    root = paths.infer_repo_root(files)
    assert root == "/Users/x/karaf"
    assert paths.relative_to(files[0][0], root) == "jaas/boot/src/main/java/o/a/A.java"


def test_infer_root_custom_layout():
    files = [
        ("/r/cassandra/./src/java/org/a/db/X.java", "org.a.db"),
        ("/r/cassandra/./tools/stress/src/org/a/s/Y.java", "org.a.s"),
    ]
    assert paths.infer_repo_root(files) == "/r/cassandra"
