# Sourced by run.sh and run_c.sh: the platform and tool record (SPEC.md).
# Needs: rec(), and the current directory at the repository root.
rec head_sha "$(git rev-parse HEAD)"
rec start_utc "$(date -u +%FT%TZ)"
rec nproc "$(nproc)"
rec uname "$(uname -a)"
rec df_before "$(df -Pk . | tail -1)"
rec cargo_lock_sha_before "$(sha256sum Cargo.lock | cut -d' ' -f1)"
rec rustc_vv "$(rustc -vV | tr '\n' ';')"
rec cargo_v "$(cargo -V)"
rec llvm_cov_version "$(cargo llvm-cov --version 2>&1)"
for tool in cargo rustc; do
  p=$(rustup which "$tool"); rec "${tool}_path" "$p"; rec "${tool}_sha256" "$(sha256sum "$p" | cut -d' ' -f1)"
done
p=$(command -v cargo-llvm-cov); rec cargo_llvm_cov_path "$p"; rec cargo_llvm_cov_sha256 "$(sha256sum "$p" | cut -d' ' -f1)"
bindir="$(rustc --print sysroot)/lib/rustlib/$(rustc -vV | sed -n 's/^host: //p')/bin"
for tool in llvm-profdata llvm-cov; do
  rec "${tool}_path" "$bindir/$tool"; rec "${tool}_sha256" "$(sha256sum "$bindir/$tool" 2>/dev/null | cut -d' ' -f1)"
done

# after the reports: remove raw profiles (flag checked first), then the closing record
finish_record() {
  if cargo llvm-cov clean --help 2>/dev/null | grep -q -- '--profraw-only'; then
    cargo llvm-cov clean --profraw-only > /dev/null 2>&1
    rec profraw_clean_rc "$?"
  else
    rec profraw_clean_rc "no-flag"
  fi
  rec cargo_lock_sha_after "$(sha256sum Cargo.lock | cut -d' ' -f1)"
  git diff --quiet -- Cargo.lock
  rec cargo_lock_git_diff_rc "$?"
  rec df_after "$(df -Pk . | tail -1)"
  rec end_utc "$(date -u +%FT%TZ)"
}
