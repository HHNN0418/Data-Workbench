import {useEffect, useState} from 'react';
import {useSearchParams} from 'react-router-dom';
import {Alert, Button, Collapse, Form, Input, Select} from 'antd';
import {Panel} from '../common';
import {readAnalysisParams, updateAnalysisSearch} from './analysisModel';
import AIAnalysis from './AIAnalysis';
import './analysis.css';

export default function AIContentAnalysis() {
 const [search,setSearch]=useSearchParams(),params=readAnalysisParams(search),[form]=Form.useForm(),[error,setError]=useState('');
 useEffect(()=>{form.setFieldsValue(params);},[search,form]);
 async function apply(){try{
  const values=await form.validateFields();
  for(const field of ['since','until'])if(values[field]){const date=new Date(values[field]);if(Number.isNaN(date.valueOf()))throw new Error('请填写有效的发布时间');values[field]=date.toISOString();}
  if(values.since&&values.until&&values.since>values.until)throw new Error('起点不能晚于截止时间');
  setSearch(updateAnalysisSearch(search,values));setError('');
 }catch(error){setError(error instanceof Error?error.message:'请检查筛选范围');}}
 const range=params.own==='own'?'本队帖子':params.own==='unknown'?'归属未知的帖子':'全部帖子';
 return <Panel title="内容分析">
  <Collapse ghost className="ai-content-scope" items={[{key:'scope',label:`帖子范围 · ${range}${params.author_id?` · 作者 #${params.author_id}`:''}${params.since||params.until?' · 已设发布时间':''}`,forceRender:true,children:<Form form={form} layout="vertical" initialValues={params} onFinish={()=>void apply()}><div className="analysis-advanced"><Form.Item name="own" label="账号范围"><Select options={[{value:'all',label:'全部账号'},{value:'own',label:'已登记本队'},{value:'unknown',label:'归属未知'}]}/></Form.Item><Form.Item name="author_id" label="作者 ID"><Input placeholder="全部作者"/></Form.Item><Form.Item name="since" label="发布起点 / ISO 时间"><Input placeholder="留空为不限"/></Form.Item><Form.Item name="until" label="发布截止 / ISO 时间"><Input placeholder="留空为不限"/></Form.Item><Button htmlType="submit">应用范围</Button></div></Form>}]} />
  {error&&<Alert type="error" title={error} showIcon/>}<AIAnalysis params={{...params,ai_view:params.ai_view||'research'}}/>
 </Panel>;
}
