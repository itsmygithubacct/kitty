// License: GPLv3 Copyright: 2023, Kovid Goyal, <kovid at kovidgoyal.net>

package main

import (
	"bufio"
	"bytes"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"
)

const (
	folder                     = "dependencies"
	fonts_folder               = "fonts"
	macos_prefix               = "/Users/Shared/kitty-build/sw/sw"
	macos_python               = "python/Python.framework/Versions/Current/bin/python3"
	macos_python_framework     = "python/Python.framework/Versions/Current/Python"
	macos_python_framework_exe = "python/Python.framework/Versions/Current/Resources/Python.app/Contents/MacOS/Python"
	NERD_URL                   = "https://github.com/ryanoasis/nerd-fonts/releases/latest/download/NerdFontsSymbolsOnly.tar.xz"
)

func root_dir() string {
	f, e := filepath.Abs(filepath.Join(folder, runtime.GOOS+"-"+runtime.GOARCH))
	if e != nil {
		exit(e)
	}
	return f
}

func fonts_dir() string {
	f, e := filepath.Abs(fonts_folder)
	if e != nil {
		exit(e)
	}
	return f
}

var _ = fmt.Print

func exit(x any) {
	switch v := x.(type) {
	case error:
		if v == nil {
			os.Exit(0)
		}
		var ee *exec.ExitError
		if errors.As(v, &ee) {
			os.Exit(ee.ExitCode())
		}
	case string:
		if v == "" {
			os.Exit(0)
		}
	case int:
		os.Exit(v)
	}
	fmt.Fprintf(os.Stderr, "\x1b[31mError\x1b[m: %s\n", x)
	os.Exit(1)
}

// download deps {{{

type dependency struct {
	path     string
	basename string
	is_id    bool
}

func lines(exe string, cmd ...string) []string {
	c := exec.Command(exe, cmd...)
	c.Stderr = os.Stderr
	out, err := c.Output()
	if err != nil {
		exit(fmt.Errorf("Failed to run '%s' with error: %w", strings.Join(append([]string{exe}, cmd...), " "), err))
	}
	ans := []string{}
	for s := bufio.NewScanner(bytes.NewReader(out)); s.Scan(); {
		ans = append(ans, s.Text())
	}
	return ans
}

func get_dependencies(path string) (ans []dependency) {
	a := lines("otool", "-D", path)
	install_name := strings.TrimSpace(a[len(a)-1])
	for _, line := range lines("otool", "-L", path) {
		line = strings.TrimSpace(line)
		if strings.Contains(line, "compatibility") && !strings.HasSuffix(line, ":") {
			before, _, _ := strings.Cut(line, "(")
			dep := strings.TrimSpace(before)
			ans = append(ans, dependency{path: dep, is_id: dep == install_name})
		}
	}
	return
}

func get_local_dependencies(path string) (ans []dependency) {
	for _, dep := range get_dependencies(path) {
		for _, y := range []string{filepath.Join(macos_prefix, "lib") + "/", filepath.Join(macos_prefix, "python", "Python.framework") + "/", "@rpath/"} {
			if strings.HasPrefix(dep.path, y) {
				if y == "@rpath/" {
					dep.basename = "lib/" + dep.path[len(y):]
				} else {
					y = macos_prefix + "/"
					dep.basename = dep.path[len(y):]
				}
				ans = append(ans, dep)
				break
			}
		}
	}
	return
}

func change_dep(path string, dep dependency) {
	cmd := []string{}
	fid := filepath.Join(root_dir(), dep.basename)
	if dep.is_id {
		cmd = append(cmd, "-id", fid)
	} else {
		cmd = append(cmd, "-change", dep.path, fid)
	}
	cmd = append(cmd, path)
	c := exec.Command("install_name_tool", cmd...)
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	if err := c.Run(); err != nil {
		exit(fmt.Errorf("Failed to run command '%s' with error: %w", strings.Join(c.Args, " "), err))
	}
}

func fix_dependencies_in_lib(path string) {
	path, err := filepath.EvalSymlinks(path)
	if err != nil {
		exit(err)
	}
	if s, err := os.Stat(path); err != nil {
		exit(err)
	} else if err := os.Chmod(path, s.Mode().Perm()|0o200); err != nil {
		exit(err)
	}
	for _, dep := range get_local_dependencies(path) {
		change_dep(path, dep)
	}
	if ldeps := get_local_dependencies(path); len(ldeps) > 0 {
		exit(fmt.Errorf("Failed to fix local dependencies in: %s", path))
	}
}

// parseContentRange extracts the start offset and total size from a Content-Range
// header of the form "bytes <start>-<end>/<total>". ok is true only when the
// <start> offset parses; total is returned as (value, true) only when <total> is a
// concrete integer, so a "*" or malformed total yields (0, false) for hasTotal
// while ok can still be true.
func parseContentRange(resp *http.Response) (start, total int64, ok, hasTotal bool) {
	cr := resp.Header.Get("Content-Range")
	if cr == "" {
		return 0, 0, false, false
	}
	// Drop the leading unit token, e.g. the "bytes " in "bytes 0-1/2".
	if _, rest, found := strings.Cut(cr, " "); found {
		cr = rest
	}
	rangePart, totalPart, found := strings.Cut(cr, "/")
	if !found {
		return 0, 0, false, false
	}
	startPart, _, found := strings.Cut(rangePart, "-")
	if !found {
		return 0, 0, false, false
	}
	start, err := strconv.ParseInt(strings.TrimSpace(startPart), 10, 64)
	if err != nil {
		return 0, 0, false, false
	}
	if t, err := strconv.ParseInt(strings.TrimSpace(totalPart), 10, 64); err == nil {
		total, hasTotal = t, true
	}
	return start, total, true, hasTotal
}

// fileSize returns the size of the file at path, or 0 if it does not exist.
func fileSize(path string) int64 {
	if fi, err := os.Stat(path); err == nil {
		return fi.Size()
	}
	return 0
}

func cached_download(url string) string {
	fname := filepath.Base(url)
	fmt.Println("Downloading", fname)
	if err := os.MkdirAll(folder, 0o755); err != nil {
		exit(err)
	}
	dest := filepath.Join(folder, fname)
	partial := dest + ".partial"
	etagFile := filepath.Join(folder, fname+".etag")

	etag := ""
	if data, err := os.ReadFile(etagFile); err == nil {
		etag = strings.TrimSpace(string(data))
	}

	const maxAttempts = 6
	client := &http.Client{}
	var lastErr error

	saveEtag := func() {
		if etag != "" {
			if err := os.WriteFile(etagFile, []byte(etag), 0o644); err != nil {
				exit(err)
			}
		}
	}
	finalize := func() string {
		if err := os.Rename(partial, dest); err != nil {
			exit(err)
		}
		saveEtag()
		return dest
	}

	for attempt := 0; attempt < maxAttempts; attempt++ {
		if attempt > 0 {
			backoff := time.Duration(attempt) * time.Second
			if backoff > 5*time.Second {
				backoff = 5 * time.Second
			}
			time.Sleep(backoff)
		}

		have := fileSize(partial)
		if have > 0 && etag == "" {
			// Without a validator we cannot prove the on-disk partial still
			// belongs to the current remote file (it may be a leftover from a
			// prior run whose remote has since changed), so a Range/206 resume
			// could silently append a suffix onto stale bytes. Only resume when
			// an etag is available to guard it with If-Range; otherwise discard
			// the partial and start over from a clean 200.
			os.Remove(partial)
			have = 0
		}

		req, err := http.NewRequest("GET", url, nil)
		if err != nil {
			exit(err)
		}
		if have == 0 {
			// A completed prior download plus a stored etag can yield a cheap 304.
			if etag != "" {
				if _, err := os.Stat(dest); err == nil {
					req.Header.Set("If-None-Match", etag)
				}
			}
		} else {
			req.Header.Set("Range", fmt.Sprintf("bytes=%d-", have))
			if etag != "" {
				// Only resume when the partial is still valid; otherwise the
				// server sends the whole file fresh (a 200), handled below.
				req.Header.Set("If-Range", etag)
			}
		}

		resp, err := client.Do(req)
		if err != nil {
			lastErr = err
			continue // transport error: retry with backoff
		}

		switch resp.StatusCode {
		case http.StatusNotModified:
			resp.Body.Close()
			return dest
		case http.StatusRequestedRangeNotSatisfiable:
			// Our partial is already >= the full size: finalize it.
			resp.Body.Close()
			if err := os.Rename(partial, dest); err != nil {
				lastErr = err
				os.Remove(partial)
				continue
			}
			saveEtag()
			return dest
		case http.StatusOK, http.StatusPartialContent:
			// handled below
		default:
			resp.Body.Close()
			exit(fmt.Errorf("The server responded with the HTTP error: %s", resp.Status))
		}

		if e := resp.Header.Get("ETag"); e != "" {
			etag = e
		}

		var expected int64
		haveExpected := false
		var f *os.File
		if resp.StatusCode == http.StatusPartialContent {
			// A 206 body carries only the [start, end] slice, so it may be
			// appended to our partial only when the server actually resumed at
			// the offset we asked for. A proxy/server that ignores Range and
			// answers 206 from a different offset would otherwise concatenate
			// overlapping or duplicate bytes into the file.
			start, total, ok, hasTotal := parseContentRange(resp)
			switch {
			case ok && start == have:
				// Genuine resume: append the suffix.
				expected, haveExpected = total, hasTotal
				f, err = os.OpenFile(partial, os.O_APPEND|os.O_WRONLY|os.O_CREATE, 0o644)
			case ok && start == 0:
				// The server ignored our Range and returned the whole file as a
				// 206 (some proxies do this). It is a complete body, so truncate
				// the partial and treat it exactly like a fresh 200.
				expected, haveExpected = total, hasTotal
				f, err = os.OpenFile(partial, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
			default:
				// Misaligned or unparseable range: a suffix from some other
				// offset cannot be reconciled with our partial. Discard it and
				// retry from scratch (have resets to 0, so the next request
				// carries no Range and the server answers with a full 200).
				resp.Body.Close()
				os.Remove(partial)
				lastErr = fmt.Errorf("server returned a 206 that does not resume at offset %d (Content-Range: %q)", have, resp.Header.Get("Content-Range"))
				continue
			}
		} else {
			// 200: the body is the whole file, so truncate any partial first.
			if resp.ContentLength >= 0 {
				expected, haveExpected = resp.ContentLength, true
			}
			f, err = os.OpenFile(partial, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
		}
		if err != nil {
			resp.Body.Close()
			exit(err)
		}

		_, copyErr := io.Copy(f, resp.Body)
		closeErr := f.Close()
		resp.Body.Close()
		if copyErr != nil {
			lastErr = copyErr
			continue // short/broken read: retry and resume
		}
		if closeErr != nil {
			exit(closeErr)
		}

		// Many truncations return copyErr == nil, so the size check is what
		// actually catches the bug.
		size := fileSize(partial)
		if haveExpected && size < expected {
			lastErr = fmt.Errorf("short read: got %d of %d bytes", size, expected)
			continue // resume from the new, larger offset
		}
		return finalize()
	}

	exit(fmt.Errorf("Failed to download %s after %d attempts: %v", fname, maxAttempts, lastErr))
	return dest
}

func relocate_pkgconfig(path, old_prefix, new_prefix string) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	nraw := bytes.ReplaceAll(raw, []byte(old_prefix), []byte(new_prefix))
	return os.WriteFile(path, nraw, 0o644)
}

func chdir_to_base() {
	_, filename, _, _ := runtime.Caller(0)
	base_dir := filepath.Dir(filepath.Dir(filename))
	if err := os.Chdir(base_dir); err != nil {
		exit(err)
	}
}

func dependencies_for_docs() {
	fmt.Println("Downloading get-pip.py")
	rq, err := http.Get("https://bootstrap.pypa.io/get-pip.py")
	if err != nil {
		exit(err)
	}
	defer rq.Body.Close()
	if rq.StatusCode != http.StatusOK {
		exit(fmt.Errorf("Server responded with HTTP error: %s", rq.Status))
	}
	gp, err := os.Create(filepath.Join(folder, "get-pip.py"))
	if err != nil {
		exit(err)
	}
	defer gp.Close()
	if _, err = io.Copy(gp, rq.Body); err != nil {
		exit(err)
	}
	python := setup_to_run_python()

	run := func(exe string, args ...string) {
		c := exec.Command(exe, args...)
		c.Stdout = os.Stdout
		c.Stderr = os.Stderr
		if err := c.Run(); err != nil {
			exit(err)
		}
	}
	run(python, gp.Name())
	run(python, "-m", "pip", "install", "-r", "docs/requirements.txt")
}

func dependencies(args []string) {
	chdir_to_base()
	nf := flag.NewFlagSet("deps", flag.ExitOnError)
	docsptr := nf.Bool("for-docs", false, "download the dependencies needed to build the documentation")
	if err := nf.Parse(args); err != nil {
		exit(err)
	}
	if *docsptr {
		dependencies_for_docs()
		fmt.Println("Dependencies needed to generate documentation have been installed. Build docs with ./dev.sh docs")
		exit(0)
	}
	data, err := os.ReadFile(".github/workflows/ci.py")
	if err != nil {
		exit(err)
	}
	pat := regexp.MustCompile("BUNDLE_URL = '(.+?)'")
	prefix := "/sw/sw"
	var url string
	if m := pat.FindStringSubmatch(string(data)); len(m) < 2 {
		exit("Failed to find BUNDLE_URL in ci.py")
	} else {
		url = m[1]
	}
	var which string
	switch runtime.GOOS {
	case "darwin":
		prefix = macos_prefix
		which = "macos"
	case "linux":
		which = "linux"
		switch runtime.GOARCH {
		case "amd64":
		case "arm64", "arm64be":
			url = strings.Replace(url, "-64.", "-arm64.", 1)
		default:
			exit(fmt.Sprintf("Pre-built binaries are not available for the %s architecture", runtime.GOARCH))
		}
	}
	if which == "" {
		exit("Prebuilt dependencies are only available for Linux and macOS")
	}
	url = strings.Replace(url, "{}", which, 1)
	if err := os.RemoveAll(root_dir()); err != nil {
		exit(err)
	}
	if err := os.MkdirAll(folder, 0o755); err != nil {
		exit(err)
	}
	tarfile, _ := filepath.Abs(cached_download(url))
	root := root_dir()
	if err := os.MkdirAll(root, 0o755); err != nil {
		exit(err)
	}
	cmd := exec.Command("tar", "xf", tarfile)
	cmd.Dir = root
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err = cmd.Run(); err != nil {
		exit(err)
	}
	if runtime.GOOS == "darwin" {
		fix_dependencies_in_lib(filepath.Join(root, macos_python))
		fix_dependencies_in_lib(filepath.Join(root, macos_python_framework))
		fix_dependencies_in_lib(filepath.Join(root, macos_python_framework_exe))
	}
	if err = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.Type().IsRegular() {
			name := d.Name()
			ext := filepath.Ext(name)
			if ext == ".pc" || (ext == ".py" && strings.HasPrefix(name, "_sysconfigdata_")) {
				err = relocate_pkgconfig(path, prefix, root)
			}
			// remove libfontconfig so that we use the system one because
			// different distros stupidly use different fontconfig configuration dirs
			if strings.HasPrefix(name, "libfontconfig.so") {
				os.Remove(path)
			}
			if runtime.GOOS == "darwin" {
				if ext == ".so" || ext == ".dylib" {
					fix_dependencies_in_lib(path)
				}
			}
		}
		return err
	}); err != nil {
		exit(err)
	}
	tarfile, _ = filepath.Abs(cached_download(NERD_URL))
	root = fonts_dir()
	if err := os.MkdirAll(root, 0o755); err != nil {
		exit(err)
	}
	cmd = exec.Command("tar", "xf", tarfile, "SymbolsNerdFontMono-Regular.ttf")
	cmd.Dir = root
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err = cmd.Run(); err != nil {
		exit(err)
	}

	fmt.Println(`Dependencies downloaded. Now build kitty with: ./dev.sh build`)
}

// }}}

func prepend(env_var, path string) {
	val := os.Getenv(env_var)
	if val != "" {
		val = string(filepath.ListSeparator) + val
	}
	os.Setenv(env_var, path+val)
}

func setup_to_run_python() (python string) {
	root := root_dir()
	for _, x := range os.Environ() {
		if strings.HasPrefix(x, "PYTHON") {
			a, _, _ := strings.Cut(x, "=")
			os.Unsetenv(a)
		}
	}
	switch runtime.GOOS {
	case "linux":
		prepend("LD_LIBRARY_PATH", filepath.Join(root, "lib"))
		os.Setenv("PYTHONHOME", root)
		python = filepath.Join(root, "bin", "python")
	case `darwin`:
		python = filepath.Join(root, macos_python)
	default:
		exit("Building is only supported on Linux and macOS")
	}
	return
}

func build(args []string) {
	chdir_to_base()
	if _, err := os.Stat(folder); err != nil {
		dependencies(nil)
	}
	root := root_dir()
	os.Setenv("DEVELOP_ROOT", root)
	prepend("PKG_CONFIG_PATH", filepath.Join(root, "lib", "pkgconfig"))
	if runtime.GOOS == "darwin" {
		os.Setenv("PKGCONFIG_EXE", filepath.Join(root, "bin", "pkg-config"))
	}
	python := setup_to_run_python()
	args = append([]string{"setup.py", "develop"}, args...)
	cmd := exec.Command(python, args...)
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	if err := cmd.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "The following build command failed:", python, strings.Join(args, " "))
		exit(err)
	}
	fmt.Println("Build successful. Run kitty as: kitty/launcher/kitty")
}

func docs(args []string) {
	setup_to_run_python()
	nf := flag.NewFlagSet("deps", flag.ExitOnError)
	livereload := nf.Bool("live-reload", false, "build the docs and make them available via s local server with live reloading for ease of development")
	failwarn := nf.Bool("fail-warn", false, "make warnings fatal when building the docs")
	if err := nf.Parse(args); err != nil {
		exit(err)
	}
	exe := filepath.Join(root_dir(), "bin", "sphinx-build")
	aexe := filepath.Join(root_dir(), "bin", "sphinx-autobuild")
	target := "docs"

	if *livereload {
		target = "develop-docs"
	}
	cmd := []string{target, "SPHINXBUILD=" + exe, "SPHINXAUTOBUILD=" + aexe}
	if *failwarn {
		cmd = append(cmd, "FAILWARN=1")
	}
	c := exec.Command("make", cmd...)
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	err := c.Run()
	if err != nil {
		exit(err)
	}
	fmt.Println("docs successfully built")
}

func main() {
	if len(os.Args) < 2 {
		exit(`Expected "deps" or "build" subcommands`)
	}
	switch os.Args[1] {
	case "deps":
		dependencies(os.Args[2:])
	case "build":
		build(os.Args[2:])
	case "docs":
		docs(os.Args[2:])
	case "-h", "--help":
		fmt.Fprintln(os.Stderr, "Usage: ./dev.sh [build|deps|docs] [options...]")
	default:
		exit(`Expected "deps" or "build" subcommands`)
	}
}
