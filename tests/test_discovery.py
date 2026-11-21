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
                (recipe/'wizard.md').write_text('---\nid: wisdom\nversion: other\nrequires: {models: {}}\n---\n')
                # A readable recipe with a bad declaration fails closed for that app only.
                self.assertIn('wisdom',router.registry()[1])
                with self.assertRaises(Fault):router.needs('wisdom')
                (recipe/'wizard.md').write_text('---\nid: wisdom\n---\n')
                self.assertNotIn('requires',router.registry()[1]['wisdom'])
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


class UnreadableRecipe(unittest.TestCase):
    """wizard-connection-router#14: one owner-only agent recipe must not block AI for every app."""
    MUSIC = ('---\nid: wizard-music\nversion: "1.0"\nrequires:\n  models:\n    curated-radio:\n'
             '      tier: fast\n      tested_on: claude-haiku-4-5\n      swap: ask\n      abilities: [text]\n---\n')

    def setup(self, root, agent_type='agent'):
        apps=[{'id':'wizard-music','urn':'wizard-music:wizardapps','commit':'a'*40,'version':'1.0','status':'running'},
              {'id':'agent-philosopher','urn':'agent-philosopher:wizardapps','commit':'c'*40,'version':'1','type':agent_type,'status':'running'},
              {'id':'agent-private','urn':'agent-private:wizardapps','commit':'d'*40,'version':'1','type':'service','status':'running'}]
        atomic_json(root/'apps.json',{'apps':apps})
        for app_id,commit,body in (('wizard-music','a'*40,self.MUSIC),('agent-philosopher','c'*40,'---\nid: agent-philosopher\n---\n'),('agent-private','d'*40,'---\nid: agent-private\n---\n')):
            recipe=root/'repos/wizardapps/apps'/app_id;recipe.mkdir(parents=True)
            (recipe/'.wizard-commit').write_text(commit);(recipe/'wizard.md').write_text(body)
        return Router(root/'apps.json',root/'data')

    def test_permission_error_is_treated_as_missing_recipe(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);router=self.setup(root)
            real=Path.read_text
            reads=[]
            def read_text(path,*a,**k):
                reads.append(str(path))
                if '/agent-' in str(path):
                    raise PermissionError(13,'Permission denied',str(path))
                return real(path,*a,**k)
            with patch.dict(os.environ,{'WIZARD_RECIPES_DIR':str(root/'repos')}),patch.object(Path,'read_text',read_text):
                with self.assertLogs('router','WARNING') as logs:
                    _,apps=router.registry()
                self.assertIn('curated-radio',apps['wizard-music']['requires']['models'])
                self.assertNotIn('requires',apps['agent-private'])
                self.assertEqual(len(router.needs('wizard-music')['models']),1)
                self.assertEqual(router.needs('agent-private')['models'],[])
            self.assertTrue(any('agent-private' in line for line in logs.output))
            # An agent with no provides/requires is skipped before any recipe read.
            self.assertFalse(any('agent-philosopher' in r for r in reads))

    @unittest.skipIf(not hasattr(os,'geteuid') or os.geteuid()==0,'root ignores file modes')
    def test_owner_only_folder_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);router=self.setup(root,agent_type='service')
            private=root/'repos/wizardapps/apps/agent-philosopher'
            os.chmod(private,0o700);os.chmod(private/'wizard.md',0o600);os.chmod(private/'.wizard-commit',0o000);os.chmod(private,0o000)
            try:
                with patch.dict(os.environ,{'WIZARD_RECIPES_DIR':str(root/'repos')}):
                    _,apps=router.registry()
                    self.assertIn('curated-radio',apps['wizard-music']['requires']['models'])
                    self.assertNotIn('requires',apps['agent-philosopher'])
            finally:
                os.chmod(private,0o700)

    def test_malformed_requires_blocks_only_that_app(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            atomic_json(root/'apps.json',{'apps':[
                {'id':'good','requires':{'models':{'look-up':{'tier':'fast','tested_on':'m','abilities':['text']}}}},
                {'id':'bad','requires':{'models':{'look-up':{'tier':'nonsense','tested_on':'m'}}}}]})
            router=Router(root/'apps.json',root/'data')
            self.assertEqual(len(router.needs('good')['models']),1)
            with self.assertRaises(Fault) as caught:router.needs('bad')
            self.assertEqual(caught.exception.code,'blocked')
