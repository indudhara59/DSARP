package dsarp.extractor;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;

/**
 * Command-line entry point of the program-model extractor.
 *
 * <pre>
 * java -jar dsarp-extractor.jar --files files.txt --out OUT_DIR
 *      [--sourcepath a:b:c] [--classpath x.jar:y.jar] [--scan-list scan.txt]
 * </pre>
 *
 * {@code files.txt} lists every source file to compile (one path per line).
 * {@code scan.txt} (optional) lists the subset to record; default: all files.
 * Output: {@code files.tsv}, {@code types.tsv}, {@code members.tsv},
 * {@code deps.tsv}, {@code diagnostics.tsv}, {@code summary.properties}.
 */
public final class Main {

    private Main() {
    }

    public static void main(String[] args) throws IOException {
        Path filesList = null;
        Path scanList = null;
        Path out = null;
        String sourcepath = "";
        String classpath = "";
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--files" -> filesList = Paths.get(args[++i]);
                case "--scan-list" -> scanList = Paths.get(args[++i]);
                case "--out" -> out = Paths.get(args[++i]);
                case "--sourcepath" -> sourcepath = args[++i];
                case "--classpath" -> classpath = args[++i];
                case "--help", "-h" -> {
                    usage();
                    return;
                }
                default -> {
                    System.err.println("Unknown argument: " + args[i]);
                    usage();
                    System.exit(2);
                }
            }
        }
        if (filesList == null || out == null) {
            usage();
            System.exit(2);
        }
        List<Path> files = readList(filesList);
        List<Path> scan = scanList == null ? files : readList(scanList);
        Files.createDirectories(out);
        long start = System.nanoTime();
        Extractor extractor = new Extractor(files, scan, sourcepath, classpath);
        Extractor.Stats stats = extractor.run(out);
        stats.seconds = (System.nanoTime() - start) / 1e9;
        stats.write(out.resolve("summary.properties"));
        System.out.printf("extracted %d files, %d types, %d members, %d dependencies in %.1f s (%d compiler errors)%n",
                stats.files, stats.types, stats.members, stats.deps, stats.seconds, stats.errors);
    }

    private static List<Path> readList(Path list) throws IOException {
        List<Path> out = new ArrayList<>();
        for (String line : Files.readAllLines(list, StandardCharsets.UTF_8)) {
            String s = line.strip();
            if (!s.isEmpty()) {
                out.add(Paths.get(s));
            }
        }
        return out;
    }

    private static void usage() {
        System.err.println("usage: java -jar dsarp-extractor.jar --files FILES.txt --out DIR "
                + "[--scan-list SCAN.txt] [--sourcepath P1:P2] [--classpath CP]");
    }
}
