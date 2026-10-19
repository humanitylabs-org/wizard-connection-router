import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from server import Router, atomic_json, Fault


class Discovery(unittest.TestCase):
    def test_recipe_requires_is_commit_and_version_fenced(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            app={'id':'wisdom','urn':'wisdom:wizardapps','commit':'a'*40,'version':'test','status':'running'}
            atomic_json(root/'apps.json',{'apps':[app]})
            recipe=root/'repos/wizardapps/apps/wisdom';recipe.mkdir(parents=True)
            (recipe/'.wizard-commit').write_text('a'*40)
            (recipe/'wizard.md').write_text('---\nid: wisdom\nversion: test\nrequires:\n  models:\n    look-up:\n      tier: fast\n      tested_on: claude-haiku-4-5\n      swap: ask\n      abilities: [text, streaming]\n---\n')
            router=Router(root/'apps.json',root/'data')
            before=(root/'apps.json').read_bytes()
            with patch.dict(os.environ,{'WIZARD_RECIPES_DIR':str(root/'repos')}):
                self.assertIn('look-up',router.registry()[1]['wisdom']['requires']['models'])
                (recipe/'.wizard-commit').write_text('b'*40)
                self.assertNotIn('requires',router.registry()[1]['wisdom'])
                (recipe/'.wizard-commit').write_text('a'*40)
                (recipe/'wizard.md').write_text('---\nid: wisdom\nversion: other\n---\n')
                with self.assertRaises(Fault):router.registry()
            self.assertEqual((root/'apps.json').read_bytes(),before)

    def test_owner_endpoint_never_enters_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);atomic_json(root/'apps.json',{'apps':[{'id':'claude','provides':['ai-connection']}]})
            atomic_json(root/'grants.json',{'enforce':True,'incoming':[],'outgoing':{},'provider_endpoints':{'claude':'http://provider:1234'}})
            router=Router(root/'apps.json',root/'data',root/'grants.json')
            self.assertEqual(router.registry()[1]['claude']['api']['internal'],'http://provider:1234')
            self.assertNotIn('provider:',(root/'apps.json').read_text())
            atomic_json(root/'grants.json',{'enforce':True,'incoming':[],'provider_endpoints':{'claude':'file:///secret'}})
            with self.assertRaises(Fault):router.registry()
