import unittest
from pathlib import Path


WORKFLOWS = Path(__file__).resolve().parents[1] / '.github' / 'workflows'


class ReleaseWorkflowTests(unittest.TestCase):
    def test_platform_outputs_remain_staged_until_both_pass(self):
        """R03: independent platform jobs cannot attach partially validated releases."""
        text = (WORKFLOWS / 'build.yml').read_text()
        platform_jobs = text.split('  publish-manifest:', 1)[0]
        self.assertNotIn('action-gh-release', platform_jobs)
        self.assertNotIn('gh release upload', platform_jobs)
        self.assertIn('needs: [test, build-macos, build-windows]', text)

    def test_homepage_writers_share_one_queue(self):
        """R01: beta and promote must prepare the page inside the same queue."""
        for name in ('build.yml', 'promote.yml'):
            text = (WORKFLOWS / name).read_text()
            self.assertIn('group: release-publication', text)
            self.assertIn('cancel-in-progress: false', text)

    def test_winget_dispatch_serializes_attempts_for_one_version(self):
        """R01: one winget version shares a queue through submission and automatic replies."""
        text = (WORKFLOWS / 'winget.yml').read_text()
        self.assertIn('group: winget-${{ inputs.release_tag }}', text)
        self.assertIn('cancel-in-progress: false', text)

    def test_promotion_checks_outputs_and_platform_receipts_before_publish(self):
        """R02: the promotion entry point uses the preflight plan before any write."""
        text = (WORKFLOWS / 'promote.yml').read_text()
        self.assertIn('macos_evidence_ref:', text)
        self.assertIn('scripts/release_publish.py promote', text)
        self.assertNotIn('gh release edit', text)
