"""Offline single-read tests: synthetic inputs only; never start real capture."""
from datetime import datetime, timezone
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from dingtalkbridge import ocr
from dingtalkbridge.config import Config
from dingtalkbridge.store import Store

ROOT=Path(__file__).resolve().parents[1]
SCENE_FIXTURE=Path(__file__).with_name('fixtures')/'ocr_once_scene.synthetic.json'
NATIVE_HELPER=ROOT/'experimental/wgc/WgcOnce.exe'

class OnceTests(unittest.TestCase):
    def scene(self):
        scene=json.loads(SCENE_FIXTURE.read_text(encoding='utf-8'))
        self.assertTrue(scene['fixture_metadata']['synthetic'])
        return scene

    def test_frozen_metadata_alternative_exact_binding(self):
        scene=self.scene(); masks=ocr._once_manual_technical(scene['candidate_scene'])
        final=ocr._once_final_masks(scene['candidate_scene'],ocr._rect(scene['candidate_scene']['message_roi_screen']))
        self.assertEqual(ocr._once_union_area(final),3300)
        self.assertEqual(len(masks),7)
        self.assertEqual(scene['candidate_scene']['calibration_gate']['technical_complete'],False)
        ocr._once_canonical_scene(scene)

    def test_mutations_reject_error_link_owner_padding_and_strong(self):
        for case in ('missing','shared','errorrow','pid','padding','mask','chain','strong'):
            with self.subTest(case=case):
                s=self.scene();c=s['candidate_scene'];a=c['alternative_technical_evidence'];o=a['owners'][0]
                if case=='missing': a['owners'].pop()
                elif case=='shared': a['owners'][1]['resolves_error_refs']=o['resolves_error_refs']
                elif case=='errorrow': o['source_error_bindings'][0]['depth']+=1
                elif case=='pid': o['owner_pid']+=1
                elif case=='padding': o['superset_padding_pixels']=7
                elif case=='mask': o['padded_superset_screen']['width']-=1
                elif case=='chain': o['ancestor_chain'][0]['parent_hwnd']='1'
                elif case=='strong': a['known_strong_input_proof'][0]['technical_bbox_owner_valid']=False
                with self.assertRaises(ocr.OcrError): ocr._once_canonical_scene(s)

    def test_scene_hash_preserves_rect_order_and_ignores_only_observation(self):
        s=self.scene();a=ocr._once_canonical_scene(s)[0]
        s['observed_at']='changed';self.assertEqual(a,ocr._once_canonical_scene(s)[0])
        s['candidate_scene']['proof_order_test']=[1,2,3,4]
        b=ocr._once_canonical_scene(s)[0];s['candidate_scene']['proof_order_test']=[4,3,2,1]
        self.assertNotEqual(b,ocr._once_canonical_scene(s)[0])

    def test_pending_and_literal_true_do_not_start_native(self):
        with tempfile.TemporaryDirectory() as d:
            cfg=Config(data_dir=Path(d))
            with Store(cfg.database_path,cfg) as store:
                for value in (False,True):
                    with patch.object(ocr,'_once_prepare',return_value=({'ready':False},{})),patch.object(ocr,'_once_native_exchange') as native:
                        result=ocr.run_user_confirmed_once(cfg,store,{'authorization_granted':value,'profile_path':'unused'})
                        self.assertEqual(result['status'],'rejected');self.assertFalse(result['pixels_read']);native.assert_not_called()

    def test_broker_store_success_uses_synthetic_factory_only(self):
        with tempfile.TemporaryDirectory() as d:
            cfg=Config(data_dir=Path(d));ready={'ready':True,'backend_digest':'a'*64,'profile_sha256':'b'*64,'scene_binding_sha256':'c'*64}
            ctx={'readiness':ready,'scene_hash':'c'*64,'roi':[0,0,200,100],'snapshot':{'windows':{'toplevel':{'hwnd':1}}}}
            event={'text':'synthetic screen context','language':'zh-Hans-CN','observed_at':datetime.now(timezone.utc).isoformat()}
            def grant(*a):
                a[2].consume();return {'nonce':'d'*64}
            with Store(cfg.database_path,cfg) as store,patch.object(ocr,'_once_prepare',return_value=(ready,ctx)),patch.object(ocr,'_once_live_parent',return_value={}),patch.object(ocr,'_once_wait_event'),patch.object(ocr,'_once_read_line',return_value='{}\n'),patch.object(ocr,'_once_validate_grant',side_effect=grant),patch.object(ocr,'_once_refresh',return_value=ctx),patch.object(ocr,'_once_native_exchange',return_value=event),patch.object(ocr,'_once_epoch'),patch.object(ocr.sys,'stdout',new=io.StringIO()):
                result=ocr.run_user_confirmed_once(cfg,store,{})
                self.assertEqual(result['status'],'completed');self.assertEqual(result['ingested_screen_contexts'],1);self.assertEqual(result['verified_message_count'],0)
                self.assertNotIn('synthetic screen context',json.dumps(result))
                status=json.loads((Path(d)/'collector-status.json').read_text())
                self.assertEqual(status['collector_session_id'],result['collector_session_id'])

    @unittest.skipUnless(os.name=='nt' and NATIVE_HELPER.is_file(),
                         'Windows compiled production guard; run scripts/Build-Native.ps1 first')
    def test_real_native_entry_rejects_forged_true_at_parent_chain(self):
        me=ocr._once_windows_process(os.getpid());now=ocr._once_filetime_now()
        request={'schema':'dingtalkbridge.wgc-once-native-request.v1','policy':'user_confirmed_roi_once',
                 'authorization_granted':True,'manual_semantic_confirmation':True,'technical_complete':True,
                 'known_strong_input_geometry_complete':True,'automatic_input_validation_complete':False,
                 'nonce':'a'*64,'backend_digest':'b'*64,'scene_binding_sha256':'c'*64,'profile_sha256':'d'*64,
                 'attempt_id':'synthetic-false-guard','created_filetime':str(now),'expires_filetime':str(now+100_000_000),
                 'ui':me,'python':me,'target':me,'backend_binding_path':'\\\\example.invalid\\never-read'}
        proc=subprocess.run([str(NATIVE_HELPER),'--live-ui-once'],input=json.dumps(request)+'\n',text=True,capture_output=True,timeout=5)
        result=json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode,0);self.assertEqual(result['guard_stage'],'os_parent_chain')
        self.assertFalse(result['capture_started']);self.assertFalse(result['pixels_read']);self.assertNotIn('text',result)

if __name__=='__main__': unittest.main()
