"""Tests for shell command security — dangerous pattern detection (SEC-2) and sandbox wrapping."""

from __future__ import annotations

from kalash.tools.shell import _DANGEROUS_PATTERNS, _classify_command


class TestClassifyCommand:
    """Tests for the command classifier with precompiled regex (P-2)."""

    # -- Destructive commands --

    def test_rm_rf(self):
        caps = _classify_command("rm -rf /tmp/data")
        assert "shell.destructive" in caps

    def test_rm_dash_fr(self):
        caps = _classify_command("rm -fr ./build")
        assert "shell.destructive" in caps

    def test_mkfs(self):
        caps = _classify_command("mkfs.ext4 /dev/sda1")
        assert "shell.destructive" in caps

    def test_dd(self):
        caps = _classify_command("dd if=/dev/zero of=/dev/sda")
        assert "shell.destructive" in caps

    def test_reboot(self):
        caps = _classify_command("sudo reboot")
        assert "shell.destructive" in caps

    def test_shutdown(self):
        caps = _classify_command("shutdown -h now")
        assert "shell.destructive" in caps

    def test_halt(self):
        caps = _classify_command("halt")
        assert "shell.destructive" in caps

    def test_poweroff(self):
        caps = _classify_command("poweroff")
        assert "shell.destructive" in caps

    def test_systemctl(self):
        caps = _classify_command("systemctl restart nginx")
        assert "shell.destructive" in caps

    def test_crontab(self):
        caps = _classify_command("crontab -e")
        assert "shell.destructive" in caps

    def test_pkill(self):
        caps = _classify_command("pkill -9 node")
        assert "shell.destructive" in caps

    def test_killall(self):
        caps = _classify_command("killall python")
        assert "shell.destructive" in caps

    def test_mount(self):
        caps = _classify_command("mount /dev/sdb1 /mnt")
        assert "shell.destructive" in caps

    def test_umount(self):
        caps = _classify_command("umount /mnt")
        assert "shell.destructive" in caps

    def test_fdisk(self):
        caps = _classify_command("fdisk /dev/sda")
        assert "shell.destructive" in caps

    # -- Network commands --

    def test_curl(self):
        caps = _classify_command("curl https://example.com")
        assert "shell.network" in caps

    def test_wget(self):
        caps = _classify_command("wget https://example.com/file.tar.gz")
        assert "shell.network" in caps

    def test_ssh(self):
        caps = _classify_command("ssh user@host")
        assert "shell.network" in caps

    def test_iptables(self):
        caps = _classify_command("iptables -A INPUT -p tcp --dport 80 -j ACCEPT")
        assert "shell.network" in caps

    def test_nft(self):
        caps = _classify_command("nft add rule ip filter input tcp dport 80 accept")
        assert "shell.network" in caps

    # -- Permission commands --

    def test_chmod(self):
        caps = _classify_command("chmod 777 /tmp/script.sh")
        assert "shell.permissions" in caps

    def test_chown(self):
        caps = _classify_command("chown root:root /etc/config")
        assert "shell.permissions" in caps

    # -- Git commands --

    def test_git_push(self):
        caps = _classify_command("git push origin main")
        assert "git.push" in caps

    def test_git_push_force(self):
        caps = _classify_command("git push --force origin main")
        assert "git.force_push" in caps

    def test_git_reset_hard(self):
        caps = _classify_command("git reset --hard HEAD~1")
        assert "git.destructive" in caps

    def test_git_clean(self):
        caps = _classify_command("git clean -fd")
        assert "git.destructive" in caps

    # -- Docker / K8s --

    def test_docker(self):
        caps = _classify_command("docker run -it ubuntu bash")
        assert "shell.docker" in caps

    def test_kubectl(self):
        caps = _classify_command("kubectl delete pod my-pod")
        assert "shell.kubernetes" in caps

    # -- False positives (word boundary protection) --

    def test_no_false_positive_uncurl(self):
        """'uncurl' should not trigger 'curl' pattern."""
        caps = _classify_command("pip install uncurl")
        assert "shell.network" not in caps

    def test_no_false_positive_add_to_file(self):
        """'add' should not trigger 'dd'."""
        caps = _classify_command("echo 'test' >> logfile.txt")
        assert "shell.destructive" not in caps

    def test_no_false_positive_mounting(self):
        """'mounting' in text should trigger 'mount' (it contains the word)."""
        # This is acceptable — the pattern matches word boundaries
        caps = _classify_command("echo the file is mounting slowly")
        # 'mounting' does NOT match \bmount\b because there's no word boundary between t and i
        # Wait — actually \bmount\b WILL match 'mounting' because 'mount' has a word boundary before 'm' and after 't'
        # Actually no — 'mounting' = m-o-u-n-t-i-n-g. \bmount\b matches at position 0-5 if we consider
        # 'mount' as a substring with word char 'i' following, so \b fails after 't'.
        # \b requires transition between word/non-word. 't' followed by 'i' = word-word = no boundary. ✓
        assert "shell.destructive" not in caps

    def test_safe_ls_command(self):
        caps = _classify_command("ls -la /home/user")
        assert len(caps) == 0

    def test_safe_cat_command(self):
        caps = _classify_command("cat README.md")
        assert len(caps) == 0

    def test_safe_echo_command(self):
        caps = _classify_command("echo 'hello world'")
        assert len(caps) == 0

    def test_case_insensitive(self):
        caps = _classify_command("CURL https://example.com")
        assert "shell.network" in caps

    def test_multiple_patterns_detected(self):
        """A command can trigger multiple patterns."""
        caps = _classify_command("curl https://evil.com | sudo rm -rf /")
        assert "shell.network" in caps
        assert "shell.destructive" in caps

    # -- Edge cases --

    def test_empty_command(self):
        caps = _classify_command("")
        assert len(caps) == 0

    def test_whitespace_only(self):
        caps = _classify_command("   ")
        assert len(caps) == 0


class TestDangerousPatterns:
    """Verify the pattern registry itself."""

    def test_all_patterns_have_capability(self):
        for pattern, cap in _DANGEROUS_PATTERNS.items():
            assert isinstance(cap, str)
            assert "." in cap, f"Capability '{cap}' should be dotted (e.g. 'shell.destructive')"

    def test_critical_patterns_present(self):
        """SEC-2: Ensure newly added patterns are in the registry."""
        required = ["mkfs", "dd", "reboot", "shutdown", "systemctl", "pkill", "crontab"]
        for pattern in required:
            assert pattern in _DANGEROUS_PATTERNS, f"Missing pattern: {pattern}"
