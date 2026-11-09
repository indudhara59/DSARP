package dsarp.extractor;

import com.sun.source.tree.AnnotationTree;
import com.sun.source.tree.BlockTree;
import com.sun.source.tree.ClassTree;
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.IdentifierTree;
import com.sun.source.tree.ImportTree;
import com.sun.source.tree.LineMap;
import com.sun.source.tree.MemberReferenceTree;
import com.sun.source.tree.MemberSelectTree;
import com.sun.source.tree.MethodInvocationTree;
import com.sun.source.tree.MethodTree;
import com.sun.source.tree.NewClassTree;
import com.sun.source.tree.Tree;
import com.sun.source.tree.VariableTree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreePath;
import com.sun.source.util.TreePathScanner;
import com.sun.source.util.Trees;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import java.util.stream.Collectors;

import javax.lang.model.element.Element;
import javax.lang.model.element.ElementKind;
import javax.lang.model.element.ExecutableElement;
import javax.lang.model.element.Modifier;
import javax.lang.model.element.PackageElement;
import javax.lang.model.element.TypeElement;
import javax.lang.model.element.TypeParameterElement;
import javax.lang.model.element.VariableElement;
import javax.lang.model.type.ArrayType;
import javax.lang.model.type.DeclaredType;
import javax.lang.model.type.IntersectionType;
import javax.lang.model.type.TypeKind;
import javax.lang.model.type.TypeMirror;
import javax.lang.model.type.WildcardType;
import javax.lang.model.util.Elements;
import javax.lang.model.util.Types;
import javax.tools.Diagnostic;
import javax.tools.DiagnosticListener;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

/**
 * Extracts the program model of a Java code base with the JDK's own compiler
 * API (javac parse + attribute), so names resolve exactly as the compiler
 * resolves them. Types missing from the classpath become "unresolved" and
 * are counted; analysis continues.
 *
 * <p>Dependencies are recorded from the innermost named element (a method,
 * constructor, field, initializer, or the type itself) to the referenced
 * type or member, but only when the target is declared in the analysed
 * sources. Each dependency has a kind and a count.
 */
final class Extractor {

    /** Counters written to summary.properties. */
    static final class Stats {
        int files;
        int types;
        int members;
        int deps;
        int errors;
        int unresolvedTypes;
        double seconds;

        void write(Path file) throws IOException {
            Properties p = new Properties();
            p.setProperty("files", Integer.toString(files));
            p.setProperty("types", Integer.toString(types));
            p.setProperty("members", Integer.toString(members));
            p.setProperty("deps", Integer.toString(deps));
            p.setProperty("compiler_errors", Integer.toString(errors));
            p.setProperty("unresolved_type_uses", Integer.toString(unresolvedTypes));
            p.setProperty("seconds", String.format("%.1f", seconds));
            p.setProperty("java_version", System.getProperty("java.version"));
            try (BufferedWriter w = Files.newBufferedWriter(file, StandardCharsets.UTF_8)) {
                p.store(w, "dsarp extractor summary");
            }
        }
    }

    private final List<Path> files;
    private final Set<Path> scan;
    private final String sourcepath;
    private final String classpath;

    private Trees trees;
    private Elements elements;
    private Types types;
    private final Stats stats = new Stats();
    private final Map<String, Integer> diagnosticCodes = new HashMap<>();
    private final List<String> diagnosticSamples = new ArrayList<>();
    /** Every compiler error: file, line, code, message (used to compare builds before and after a plan). */
    private final List<String[]> errors = new ArrayList<>();
    private final Map<String, Integer> unresolvedNames = new HashMap<>();
    private final Map<TypeElement, Boolean> sourceCache = new HashMap<>();
    /** key "src\tdst\tkind" -> {count, first line}. */
    private final Map<String, int[]> deps = new LinkedHashMap<>();

    private BufferedWriter typesOut;
    private BufferedWriter membersOut;
    private BufferedWriter filesOut;

    Extractor(List<Path> files, List<Path> scan, String sourcepath, String classpath) {
        this.files = files;
        this.scan = new HashSet<>();
        for (Path p : scan) {
            this.scan.add(p.toAbsolutePath().normalize());
        }
        this.sourcepath = sourcepath;
        this.classpath = classpath;
    }

    Stats run(Path out) throws IOException {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            throw new IllegalStateException("No Java compiler found: run with a JDK, not a JRE");
        }
        DiagnosticListener<JavaFileObject> listener = this::onDiagnostic;
        StandardJavaFileManager fm = compiler.getStandardFileManager(listener, null, StandardCharsets.UTF_8);
        Iterable<? extends JavaFileObject> units = fm.getJavaFileObjectsFromPaths(files);
        List<String> options = new ArrayList<>(List.of(
                "-proc:none", "-implicit:none", "-Xlint:none", "-nowarn", "-encoding", "UTF-8",
                // keep attributing even when external libraries are missing
                "-XDshould-stop.ifError=FLOW", "-XDshould-stop.ifNoError=FLOW",
                "-Xmaxerrs", "100000"));
        options.add("-sourcepath");
        options.add(sourcepath.isEmpty() ? "" : sourcepath);
        if (!classpath.isEmpty()) {
            options.add("-classpath");
            options.add(classpath);
        }
        JavacTask task = (JavacTask) compiler.getTask(null, fm, listener, options, null, units);
        Iterable<? extends CompilationUnitTree> cus = task.parse();
        task.analyze();
        trees = Trees.instance(task);
        elements = task.getElements();
        types = task.getTypes();

        try (BufferedWriter t = open(out, "types.tsv",
                "type\tkind\tpackage\ttop_level\touter\tfile\tline\tend_line\tvisibility\tabstract\tfinal\tstatic\tdeprecated");
             BufferedWriter m = open(out, "members.tsv",
                     "member\tkind\towner\tname\tvisibility\tstatic\tabstract\tfinal\tdeprecated\toverride\tparams\tline\tend_line");
             BufferedWriter f = open(out, "files.tsv", "file\tpackage\ttop_level_types\tlines\timports")) {
            typesOut = t;
            membersOut = m;
            filesOut = f;
            for (CompilationUnitTree cu : cus) {
                Path p = Path.of(cu.getSourceFile().toUri()).toAbsolutePath().normalize();
                if (scan.contains(p)) {
                    new Scanner(cu).scan(cu, null);
                    stats.files++;
                }
            }
        }
        writeDeps(out);
        writeDiagnostics(out);
        return stats;
    }

    // ------------------------------------------------------------------ output

    private static BufferedWriter open(Path dir, String name, String header) throws IOException {
        BufferedWriter w = Files.newBufferedWriter(dir.resolve(name), StandardCharsets.UTF_8);
        w.write(header);
        w.newLine();
        return w;
    }

    private static String clean(Object v) {
        return v == null ? "" : v.toString().replace('\t', ' ').replace('\n', ' ').replace('\r', ' ');
    }

    private static void row(BufferedWriter w, Object... values) {
        try {
            for (int i = 0; i < values.length; i++) {
                if (i > 0) {
                    w.write('\t');
                }
                w.write(clean(values[i]));
            }
            w.newLine();
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    private void writeDeps(Path out) throws IOException {
        try (BufferedWriter w = open(out, "deps.tsv", "source\ttarget\tkind\tcount\tline")) {
            for (Map.Entry<String, int[]> e : deps.entrySet()) {
                w.write(e.getKey());
                w.write('\t');
                w.write(Integer.toString(e.getValue()[0]));
                w.write('\t');
                w.write(Integer.toString(e.getValue()[1]));
                w.newLine();
            }
        }
        stats.deps = deps.size();
    }

    private void writeDiagnostics(Path out) throws IOException {
        try (BufferedWriter w = open(out, "diagnostics.tsv", "kind\tkey\tcount")) {
            for (Map.Entry<String, Integer> e : diagnosticCodes.entrySet()) {
                row(w, "compiler", e.getKey(), e.getValue());
            }
            List<Map.Entry<String, Integer>> top = unresolvedNames.entrySet().stream()
                    .sorted((a, b) -> b.getValue() - a.getValue()).limit(200).collect(Collectors.toList());
            for (Map.Entry<String, Integer> e : top) {
                row(w, "unresolved_type", e.getKey(), e.getValue());
            }
            for (String s : diagnosticSamples) {
                row(w, "sample", s, 1);
            }
        }
        Path cwd = Path.of("").toAbsolutePath().normalize();
        try (BufferedWriter w = open(out, "errors.tsv", "file\tline\tcode\tmessage")) {
            for (String[] e : errors) {
                String file = e[0];
                if (!file.isEmpty()) {
                    Path p = Path.of(file).toAbsolutePath().normalize();
                    file = p.startsWith(cwd) ? cwd.relativize(p).toString().replace('\\', '/') : p.toString();
                }
                w.write(clean(file) + "\t" + e[1] + "\t" + clean(e[2]) + "\t" + clean(e[3]));
                w.newLine();
            }
        }
    }

    private static String clean(String s) {
        return s.replace('\t', ' ').replace('\n', ' ').replace('\r', ' ');
    }

    private void onDiagnostic(Diagnostic<? extends JavaFileObject> d) {
        if (d.getKind() != Diagnostic.Kind.ERROR) {
            return;
        }
        stats.errors++;
        diagnosticCodes.merge(d.getCode(), 1, Integer::sum);
        String file = d.getSource() == null ? "" : Path.of(d.getSource().toUri()).toString();
        errors.add(new String[] {file, Long.toString(d.getLineNumber()), d.getCode(), d.getMessage(null)});
        if (diagnosticSamples.size() < 50) {
            String src = d.getSource() == null ? "" : d.getSource().getName();
            diagnosticSamples.add(src + ":" + d.getLineNumber() + " " + d.getMessage(null));
        }
    }

    // ------------------------------------------------------------------ helpers

    private boolean isSource(TypeElement te) {
        TypeElement top = topLevel(te);
        return sourceCache.computeIfAbsent(top, k -> trees.getPath(k) != null);
    }

    private static TypeElement topLevel(TypeElement te) {
        Element e = te;
        TypeElement last = te;
        while (e != null && !(e instanceof PackageElement)) {
            if (e instanceof TypeElement t) {
                last = t;
            }
            e = e.getEnclosingElement();
        }
        return last;
    }

    private static TypeElement owner(Element e) {
        Element x = e.getEnclosingElement();
        while (x != null && !(x instanceof TypeElement)) {
            x = x.getEnclosingElement();
        }
        return (TypeElement) x;
    }

    private static boolean named(TypeElement te) {
        return te != null && !te.getQualifiedName().toString().isEmpty();
    }

    private String memberId(Element e) {
        TypeElement o = owner(e);
        String ownerName = o == null ? "?" : o.getQualifiedName().toString();
        if (e instanceof ExecutableElement ee) {
            String name = ee.getKind() == ElementKind.CONSTRUCTOR ? "<init>"
                    : ee.getKind() == ElementKind.STATIC_INIT ? "<clinit>"
                    : ee.getKind() == ElementKind.INSTANCE_INIT ? "<init-block>" : ee.getSimpleName().toString();
            String params = ee.getParameters().stream()
                    .map(p -> types.erasure(p.asType()).toString())
                    .collect(Collectors.joining(","));
            return ownerName + "#" + name + "(" + params + ")";
        }
        return ownerName + "#" + e.getSimpleName();
    }

    private static String visibility(Element e) {
        Set<Modifier> m = e.getModifiers();
        if (m.contains(Modifier.PUBLIC)) {
            return "public";
        }
        if (m.contains(Modifier.PROTECTED)) {
            return "protected";
        }
        if (m.contains(Modifier.PRIVATE)) {
            return "private";
        }
        return "package";
    }

    private static String flag(boolean b) {
        return b ? "1" : "0";
    }

    private void addDep(String source, String target, String kind, int line) {
        if (source == null || target == null || source.equals(target)) {
            return;
        }
        int[] v = deps.computeIfAbsent(source + "\t" + target + "\t" + kind, k -> new int[] {0, line});
        v[0]++;
    }

    /** Dependency on a type (only if it is declared in the analysed sources). */
    private void typeDep(String source, TypeElement te, String kind, int line) {
        if (te == null) {
            return;
        }
        TypeElement named = te;
        while (named != null && !named(named)) {
            named = owner(named);
        }
        if (named != null && isSource(named)) {
            addDep(source, named.getQualifiedName().toString(), kind, line);
        }
    }

    /** Dependency on a method, constructor or field of a source type. */
    private void memberDep(String source, Element member, String kind, int line) {
        TypeElement o = owner(member);
        if (o == null || !named(o) || !isSource(o)) {
            return;
        }
        addDep(source, memberId(member), kind, line);
    }

    /** Dependencies on every source type mentioned in a type (generics, arrays, bounds). */
    private void typeMirrorDeps(String source, TypeMirror tm, String kind, int line, int depth) {
        if (tm == null || depth > 6) {
            return;
        }
        switch (tm.getKind()) {
            case DECLARED -> {
                DeclaredType dt = (DeclaredType) tm;
                typeDep(source, (TypeElement) dt.asElement(), kind, line);
                for (TypeMirror arg : dt.getTypeArguments()) {
                    typeMirrorDeps(source, arg, "type_arg", line, depth + 1);
                }
            }
            case ARRAY -> typeMirrorDeps(source, ((ArrayType) tm).getComponentType(), kind, line, depth + 1);
            case WILDCARD -> {
                WildcardType w = (WildcardType) tm;
                typeMirrorDeps(source, w.getExtendsBound(), "type_arg", line, depth + 1);
                typeMirrorDeps(source, w.getSuperBound(), "type_arg", line, depth + 1);
            }
            case INTERSECTION -> {
                for (TypeMirror b : ((IntersectionType) tm).getBounds()) {
                    typeMirrorDeps(source, b, kind, line, depth + 1);
                }
            }
            case ERROR -> {
                stats.unresolvedTypes++;
                unresolvedNames.merge(tm.toString(), 1, Integer::sum);
            }
            default -> {
                // primitives, type variables, void: nothing to record
            }
        }
    }

    // ------------------------------------------------------------------ scanner

    private final class Scanner extends TreePathScanner<Void, Void> {
        private final CompilationUnitTree cu;
        private final SourcePositions positions;
        private final LineMap lines;
        private final String file;
        private final Deque<TypeElement> typeStack = new ArrayDeque<>();
        private String current;
        private final List<String> topLevel = new ArrayList<>();
        private int imports;

        Scanner(CompilationUnitTree cu) {
            this.cu = cu;
            this.positions = trees.getSourcePositions();
            this.lines = cu.getLineMap();
            this.file = cu.getSourceFile().getName();
        }

        private int line(Tree t) {
            long pos = positions.getStartPosition(cu, t);
            return pos < 0 ? 0 : (int) lines.getLineNumber(pos);
        }

        private int endLine(Tree t) {
            long pos = positions.getEndPosition(cu, t);
            return pos < 0 ? 0 : (int) lines.getLineNumber(pos);
        }

        private Element element(Tree t) {
            return trees.getElement(new TreePath(getCurrentPath(), t));
        }

        @Override
        public Void visitCompilationUnit(CompilationUnitTree node, Void unused) {
            for (Tree decl : node.getTypeDecls()) {
                Element e = element(decl);
                if (e instanceof TypeElement te && named(te)) {
                    topLevel.add(te.getQualifiedName().toString());
                }
            }
            super.visitCompilationUnit(node, unused);
            String pkg = node.getPackageName() == null ? "" : node.getPackageName().toString();
            long end = positions.getEndPosition(node, node);
            int total = end < 0 ? 0 : (int) lines.getLineNumber(end);
            row(filesOut, file, pkg, String.join(";", topLevel), total, imports);
            return null;
        }

        @Override
        public Void visitImport(ImportTree node, Void unused) {
            imports++;
            Tree qid = node.getQualifiedIdentifier();
            Element e = element(qid);
            int ln = line(node);
            if (e == null && node.isStatic() && qid instanceof MemberSelectTree ms) {
                e = element(ms.getExpression());  // static import of overloaded methods: use the owner type
            }
            if (e == null) {
                return null;
            }
            for (String src : topLevel) {
                if (e instanceof TypeElement te) {
                    typeDep(src, te, "import", ln);
                } else if (e.getKind() == ElementKind.FIELD || e.getKind() == ElementKind.METHOD
                        || e.getKind() == ElementKind.ENUM_CONSTANT) {
                    memberDep(src, e, "import", ln);
                }
            }
            return null;  // do not count the import's identifiers again as references
        }

        @Override
        public Void visitClass(ClassTree node, Void unused) {
            Element e = trees.getElement(getCurrentPath());
            if (!(e instanceof TypeElement te) || !named(te)) {
                return super.visitClass(node, unused);  // anonymous/local: deps go to the enclosing member
            }
            String name = te.getQualifiedName().toString();
            TypeElement outer = owner(te);
            Set<Modifier> mods = te.getModifiers();
            row(typesOut, name, te.getKind().name().toLowerCase(), elements.getPackageOf(te).getQualifiedName(),
                    topLevel(te).getQualifiedName(), outer == null ? "" : outer.getQualifiedName(),
                    file, line(node), endLine(node), visibility(te), flag(mods.contains(Modifier.ABSTRACT)),
                    flag(mods.contains(Modifier.FINAL)), flag(mods.contains(Modifier.STATIC)),
                    flag(elements.isDeprecated(te)));
            stats.types++;

            int ln = line(node);
            boolean isInterface = te.getKind().isInterface();
            TypeMirror sup = te.getSuperclass();
            if (sup != null && sup.getKind() != TypeKind.NONE) {
                typeMirrorDeps(name, sup, "extends", ln, 0);
            }
            for (TypeMirror i : te.getInterfaces()) {
                typeMirrorDeps(name, i, isInterface ? "extends" : "implements", ln, 0);
            }
            for (TypeParameterElement tp : te.getTypeParameters()) {
                for (TypeMirror b : tp.getBounds()) {
                    typeMirrorDeps(name, b, "type_bound", ln, 0);
                }
            }

            String saved = current;
            current = name;
            typeStack.push(te);
            scan(node.getModifiers(), null);   // annotations on the type
            scan(node.getMembers(), null);     // extends/implements already recorded from the element
            typeStack.pop();
            current = saved;
            return null;
        }

        private boolean recordedOwner(Element member) {
            TypeElement o = owner(member);
            return named(o) && !typeStack.isEmpty() && typeStack.peek().equals(o);
        }

        @Override
        public Void visitMethod(MethodTree node, Void unused) {
            Element e = trees.getElement(getCurrentPath());
            if (!(e instanceof ExecutableElement ee) || !recordedOwner(ee)) {
                return super.visitMethod(node, unused);
            }
            String id = memberId(ee);
            Set<Modifier> mods = ee.getModifiers();
            boolean override = ee.getAnnotationMirrors().stream()
                    .anyMatch(a -> a.getAnnotationType().toString().equals("java.lang.Override"));
            row(membersOut, id, ee.getKind() == ElementKind.CONSTRUCTOR ? "constructor" : "method",
                    owner(ee).getQualifiedName(), ee.getSimpleName(), visibility(ee),
                    flag(mods.contains(Modifier.STATIC)), flag(mods.contains(Modifier.ABSTRACT)),
                    flag(mods.contains(Modifier.FINAL)), flag(elements.isDeprecated(ee)), flag(override),
                    ee.getParameters().size(), line(node), endLine(node));
            stats.members++;

            int ln = line(node);
            typeMirrorDeps(id, ee.getReturnType(), "return_type", ln, 0);
            for (VariableElement p : ee.getParameters()) {
                typeMirrorDeps(id, p.asType(), "param_type", ln, 0);
            }
            for (TypeMirror t : ee.getThrownTypes()) {
                typeMirrorDeps(id, t, "throws", ln, 0);
            }
            for (TypeParameterElement tp : ee.getTypeParameters()) {
                for (TypeMirror b : tp.getBounds()) {
                    typeMirrorDeps(id, b, "type_bound", ln, 0);
                }
            }
            String saved = current;
            current = id;
            scan(node.getModifiers(), null);
            scan(node.getBody(), null);
            scan(node.getDefaultValue(), null);
            current = saved;
            return null;
        }

        @Override
        public Void visitVariable(VariableTree node, Void unused) {
            Element e = trees.getElement(getCurrentPath());
            if (e == null) {
                return super.visitVariable(node, unused);
            }
            ElementKind k = e.getKind();
            if ((k == ElementKind.FIELD || k == ElementKind.ENUM_CONSTANT) && recordedOwner(e)) {
                String id = memberId(e);
                Set<Modifier> mods = e.getModifiers();
                row(membersOut, id, k == ElementKind.FIELD ? "field" : "enum_constant",
                        owner(e).getQualifiedName(), e.getSimpleName(), visibility(e),
                        flag(mods.contains(Modifier.STATIC)), "0", flag(mods.contains(Modifier.FINAL)),
                        flag(elements.isDeprecated(e)), "0", 0, line(node), endLine(node));
                stats.members++;
                if (k == ElementKind.FIELD) {
                    typeMirrorDeps(id, e.asType(), "field_type", line(node), 0);
                }
                String saved = current;
                current = id;
                scan(node.getModifiers(), null);
                scan(node.getInitializer(), null);
                current = saved;
                return null;
            }
            if (k == ElementKind.PARAMETER) {
                scan(node.getModifiers(), null);
                return null;  // parameter types are recorded from the method element
            }
            // local variables, resources, catch parameters, pattern bindings
            typeMirrorDeps(current, e.asType(), "local_type", line(node), 0);
            scan(node.getModifiers(), null);
            scan(node.getInitializer(), null);
            return null;
        }

        @Override
        public Void visitBlock(BlockTree node, Void unused) {
            TreePath parent = getCurrentPath().getParentPath();
            if (parent != null && parent.getLeaf() instanceof ClassTree && !typeStack.isEmpty()) {
                TypeElement o = typeStack.peek();
                String id = o.getQualifiedName() + (node.isStatic() ? "#<clinit>()" : "#<init-block>()");
                row(membersOut, id, "initializer", o.getQualifiedName(), node.isStatic() ? "<clinit>" : "<init-block>",
                        "private", flag(node.isStatic()), "0", "0", "0", "0", 0, line(node), endLine(node));
                stats.members++;
                String saved = current;
                current = id;
                super.visitBlock(node, unused);
                current = saved;
                return null;
            }
            return super.visitBlock(node, unused);
        }

        @Override
        public Void visitNewClass(NewClassTree node, Void unused) {
            Element ctor = trees.getElement(getCurrentPath());
            int ln = line(node);
            if (node.getClassBody() != null) {
                // anonymous class: the dependency is on the class or interface it extends
                Element base = element(node.getIdentifier());
                if (base instanceof TypeElement te) {
                    typeDep(current, te, "new", ln);
                }
            } else if (ctor instanceof ExecutableElement) {
                typeDep(current, owner(ctor), "new", ln);
                memberDep(current, ctor, "call", ln);
            }
            scan(node.getEnclosingExpression(), null);
            scan(node.getTypeArguments(), null);
            scan(node.getArguments(), null);
            scan(node.getClassBody(), null);  // anonymous class body: deps stay with the current member
            return null;
        }

        @Override
        public Void visitMethodInvocation(MethodInvocationTree node, Void unused) {
            Element m = trees.getElement(getCurrentPath());
            if (m instanceof ExecutableElement) {
                memberDep(current, m, "call", line(node));
            }
            if (node.getMethodSelect() instanceof MemberSelectTree ms) {
                scan(ms.getExpression(), null);  // the receiver
            }
            scan(node.getTypeArguments(), null);
            scan(node.getArguments(), null);
            return null;
        }

        @Override
        public Void visitMemberReference(MemberReferenceTree node, Void unused) {
            Element m = trees.getElement(getCurrentPath());
            if (m instanceof ExecutableElement) {
                memberDep(current, m, "method_ref", line(node));
            }
            scan(node.getQualifierExpression(), null);
            return null;
        }

        @Override
        public Void visitAnnotation(AnnotationTree node, Void unused) {
            Element a = element(node.getAnnotationType());
            if (a instanceof TypeElement te) {
                typeDep(current, te, "annotation", line(node));
            }
            scan(node.getArguments(), null);
            return null;
        }

        private void reference(Element e, Tree node) {
            if (e == null || current == null) {
                return;
            }
            switch (e.getKind()) {
                case CLASS, INTERFACE, ENUM, RECORD, ANNOTATION_TYPE -> typeDep(current, (TypeElement) e, "type_ref", line(node));
                case FIELD, ENUM_CONSTANT -> memberDep(current, e, "field_access", line(node));
                default -> {
                    // locals, parameters, packages, methods (handled elsewhere)
                }
            }
        }

        @Override
        public Void visitIdentifier(IdentifierTree node, Void unused) {
            reference(trees.getElement(getCurrentPath()), node);
            return null;
        }

        @Override
        public Void visitMemberSelect(MemberSelectTree node, Void unused) {
            Element e = trees.getElement(getCurrentPath());
            reference(e, node);
            if (e == null || e.getKind() == ElementKind.FIELD || e.getKind() == ElementKind.ENUM_CONSTANT
                    || e.getKind() == ElementKind.METHOD) {
                scan(node.getExpression(), null);  // receiver of a field access
            }
            return null;
        }
    }
}
