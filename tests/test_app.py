import importlib
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        os.environ['INTERVIEW_DATA_DIR']=cls.temp.name
        import app
        cls.app=app
        cls.client=TestClient(app.app)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()
        os.environ.pop('INTERVIEW_DATA_DIR',None)

    def test_a_seed_count_answers_and_gate(self):
        with self.app.db() as c:
            counts={r['section']:r['n'] for r in self.app.rows(c,'SELECT section,COUNT(*) n FROM groups GROUP BY section')}
            self.assertEqual(counts,{'Java 高频八股':20,'后端场景':5,'Agent 场景':5,'HR 通用':10})
            self.assertEqual(self.app.one(c,"SELECT COUNT(*) n FROM answers WHERE primary_text!=''")['n'],40)
        self.app.seed_bank()
        with self.app.db() as c:
            self.assertEqual(self.app.one(c,'SELECT COUNT(*) n FROM groups')['n'],40)
        self.assertEqual(self.client.get('/api/setup/status').status_code,200)
        with patch.object(self.app,'key_get',return_value=''):
            self.assertEqual(self.client.get('/api/banks/network/groups').status_code,403)
            self.assertEqual(self.client.post('/api/practice/draw').status_code,403)
        with patch.object(self.app,'key_get',return_value='fake'):
            self.assertEqual(self.client.get('/api/banks/network/groups').json()['total'],40)
            self.assertEqual(self.client.post('/api/resume/skip').status_code,200)
            self.assertEqual(self.client.get('/api/setup/status').json()['resume_skipped'],True)
            self.assertEqual(self.client.post('/api/practice/ai-question',json={'category':'project'}).status_code,409)
            with patch.object(self.app,'ai_json',return_value={'text':'Java 线程池拒绝策略如何选择？','answer':'先定义队列和线程边界，再根据业务选择拒绝或降级。'}):
                generated=self.client.post('/api/practice/ai-question',json={'category':'fundamentals'})
                self.assertEqual(generated.status_code,200)
                self.assertIn('线程池',generated.json()['current']['text'])
                reveal=self.client.post('/api/practice/'+generated.json()['current']['delivery_id']+'/reveal').json()['current']
                self.assertTrue(reveal['answer']['primary_text'])
            draw=self.client.post('/api/practice/draw').json()['current']
            self.assertTrue(draw['text'])
            self.assertTrue(draw['text'])

    def test_b_resume_version_and_stale_answers(self):
        with patch.object(self.app,'key_get',return_value='fake'):
            payload={'facts':{'target_role':'Java 后端','skills':['Java'],'projects':[{'name':'示例项目','role':'开发','stack':'Java','contributions':'实现接口','results':''}]}}
            self.assertEqual(self.client.put('/api/resume/confirm',json=payload).json()['version'],1)
            with self.app.db() as c:
                gid=self.app.insert_group(c,{'text':'示例项目为什么选择该方案？','topic':'java','section':'项目深挖','interview_type':'technical','source_key':'test:project','provenance':'ai'},'ai')
                c.execute('UPDATE groups SET resume_version=1 WHERE id=?',(gid,))
                q=self.app.one(c,'SELECT id FROM questions WHERE group_id=?',(gid,))
                c.execute("UPDATE answers SET primary_text='旧答案',primary_origin='ai',resume_version=1 WHERE question_id=?",(q['id'],))
            payload['facts']['skills']=['Java','Spring']
            self.assertEqual(self.client.put('/api/resume/confirm',json=payload).json()['version'],2)
            with self.app.db() as c:
                self.assertTrue(self.app.answer(c,q['id'])['stale_resume'])
                self.assertEqual(self.app.one(c,'SELECT primary_text FROM answers WHERE question_id=?',(q['id'],))['primary_text'],'旧答案')
                self.assertTrue(self.app.resume_projects(c))
            self.assertEqual(self.client.delete('/api/resume').status_code,200)
            with self.app.db() as c:
                self.assertTrue(self.app.answer(c,q['id'])['stale_resume'])
            self.assertEqual(self.client.put('/api/resume/confirm',json=payload).json()['version'],3)
            from fastapi import HTTPException
            with patch.object(self.app,'ai_json',side_effect=HTTPException(503,detail={'code':'MODEL_UNAVAILABLE','message':'重试'})):
                failed=self.client.post('/api/questions/'+q['id']+'/answer/regenerate')
            self.assertEqual(failed.status_code,503)
            with self.app.db() as c:
                self.assertEqual(self.app.answer(c,q['id'])['primary_text'],'旧答案')
            with patch.object(self.app,'ai_json',return_value={'answer':'按当前事实重新给出的答案，仍需用户核对。'}):
                refreshed=self.client.post('/api/questions/'+q['id']+'/answer/regenerate')
            self.assertEqual(refreshed.status_code,200)
            self.assertFalse(refreshed.json()['answer']['stale_answer'])
            self.assertTrue(refreshed.json()['answer']['stale_question'])

    def test_d_manual_import_preview_and_restore(self):
        with patch.object(self.app,'key_get',return_value='fake'):
            single=self.client.post('/api/banks/personal/questions',json={'text':'Java 并发写入怎样保证原子性？','answer':'使用锁或原子类，并确认复合操作边界。','topic':'java'})
            self.assertEqual(single.status_code,200)
            gid=single.json()['group_id']
            self.assertEqual(self.client.delete('/api/groups/'+gid).status_code,200)
            self.assertEqual(self.client.post('/api/groups/'+gid+'/restore').status_code,200)
            parsed={'groups':[{'text':'Redis 缓存击穿如何处理？','answer':'可用互斥重建与限流保护数据库。','followups':[{'text':'热点键失效时怎么限制回源？','answer':'单飞或互斥重建。'}],'interview_type':'technical','topic':'java'}]}
            with patch.object(self.app,'ai_json',return_value=parsed):
                preview=self.client.post('/api/imports/network/preview',json={'text':'Redis 缓存击穿如何处理？热点键失效时怎么限制回源？'})
            self.assertEqual(preview.status_code,200)
            job=preview.json()['job_id']
            candidates=self.client.get('/api/imports/'+job+'/candidates').json()['candidates']
            self.assertEqual(len(candidates),1)
            imported=self.client.post('/api/imports/'+job+'/candidates/'+candidates[0]['id']+'/import')
            self.assertEqual(imported.status_code,200)
            self.assertTrue(imported.json()['finished'])
            group=self.client.get('/api/groups/'+imported.json()['group_id']).json()
            self.assertEqual(len(group['questions']),2)

    def test_c_resume_redaction_and_rejected_files(self):
        with patch.object(self.app,'key_get',return_value='fake'):
            text='Java 后端开发经历，参与订单服务，负责接口实现和性能排查。联系电话：13812345678，邮箱：me@example.com。项目成果经过压测验证。'
            data=self.client.post('/api/resume/prepare',json={'text':text}).json()['text_to_send']
            self.assertNotIn('13812345678',data)
            self.assertNotIn('me@example.com',data)
            bad=self.client.post('/api/resume/extract',files={'file':('resume.txt',b'x'*150,'text/plain')})
            self.assertEqual(bad.status_code,400)
            from io import BytesIO
            from docx import Document
            doc=Document();doc.add_paragraph('Java 后端开发。参与订单服务接口实现，负责数据校验和异常处理；通过日志定位并修复过超时问题。')
            buffer=BytesIO();doc.save(buffer)
            parsed=self.client.post('/api/resume/extract',files={'file':('resume.docx',buffer.getvalue(),'application/vnd.openxmlformats-officedocument.wordprocessingml.document')})
            self.assertEqual(parsed.status_code,200)
            self.assertIn('订单服务',parsed.json()['text'])
            from pypdf import PdfWriter
            pdf=PdfWriter();pdf.add_blank_page(width=300,height=300)
            buffer=BytesIO();pdf.write(buffer)
            scanned=self.client.post('/api/resume/extract',files={'file':('scan.pdf',buffer.getvalue(),'application/pdf')})
            self.assertEqual(scanned.status_code,400)
            self.assertEqual(scanned.json()['code'],'NO_TEXT')


if __name__=='__main__': unittest.main()
