import {useEffect, useState} from 'react';
import {Link,useSearchParams} from 'react-router-dom';
import {useQueryClient} from '@tanstack/react-query';
import {Alert, Button, Empty, Input, Modal, Radio, Select, Skeleton, Space, Switch, Table, Tabs, Tag} from 'antd';
import {useAction, useApi, type Row} from '../api';
import {time} from '../common';
import {concernLabels} from '../labels';
import {providerReady} from './aiProviders';
import './AIReview.css';

type FactSettings = {review_mode:'manual'|'auto';extract_enabled:boolean;provider_id:string;pipeline:'single'|'staged';revision:number};
type Overview = {settings:FactSettings;providers:Row[];concerns:string[];topics:string[]};
type Editable = Pick<FactSettings,'review_mode'|'extract_enabled'|'provider_id'|'pipeline'>;
const editable=(s:FactSettings):Editable=>({review_mode:s.review_mode,extract_enabled:s.extract_enabled,provider_id:s.provider_id||'',pipeline:s.pipeline||'single'});
const same=(a:Editable,b:Editable)=>a.review_mode===b.review_mode&&a.extract_enabled===b.extract_enabled&&a.provider_id===b.provider_id&&a.pipeline===b.pipeline;
// Shared with the stage pages; kept in labels.ts so they do not load this page's chunk.
export {concernLabels};
const statusLabels:Record<string,string>={pending:'待审',active:'生效',rejected:'已拒绝',superseded:'已被取代',retired:'已停用'};
const statusColors:Record<string,string>={pending:'orange',active:'green',rejected:'red',superseded:'default',retired:'default'};
const available=(p?:Row)=>!!p&&(typeof p.ready==='boolean'?p.ready&&p.enabled!==false:providerReady(p as any));
const label=(c:string)=>concernLabels[c]||c;
const message=(cause:unknown)=>cause instanceof Error?cause.message:'操作失败，请重试';

function Settings({data,onSaved}:{data:Overview;onSaved:()=>void}){
 const save=useAction<Overview>('/agents/facts/settings');
 const [baseline,setBaseline]=useState(data.settings),[form,setForm]=useState(editable(data.settings)),[error,setError]=useState(''),[saved,setSaved]=useState(false);
 useEffect(()=>{if(same(form,editable(baseline))){setBaseline(data.settings);setForm(editable(data.settings));}},[data.settings]);
 const dirty=!same(form,editable(baseline)),selected=data.providers.find(p=>p.id===form.provider_id);
 const invalid=form.extract_enabled&&!available(selected),conflict=data.settings.revision!==baseline.revision;
 const change=(values:Partial<Editable>)=>{setForm(current=>({...current,...values}));setError('');setSaved(false);};
 async function persist(){
  try{const result=await save.mutateAsync({...form,expected_revision:baseline.revision});setBaseline(result.settings);setForm(editable(result.settings));setSaved(true);onSaved();}
  catch(cause){setError(message(cause));onSaved();}
 }
 return <section className="panel ai-review-settings" aria-label="事实库设置">
  <div className="ai-review-control"><div><h3>Agent 写作流程</h3><p className="muted">分段流程：先决定对谁说、说什么并选出事实，再由写作步骤按事实写正文，最后检查正文方向与事实依据，不合格重写一次。原流程：一次调用同时决定和写作，不使用事实库。随时可切回。</p></div>
   <Radio.Group aria-label="Agent 写作流程" value={form.pipeline} disabled={save.isPending} onChange={event=>change({pipeline:event.target.value})} optionType="button" options={[{value:'staged',label:'分段（决策→写作→检查）'},{value:'single',label:'原流程'}]}/></div>
  <div className="ai-review-control"><div><h3>新闻事实审核方式</h3><p className="muted">与发文审核相同：人工审核时，新提取的事实先进入待审，通过后 Agent 才能引用；自动生效时，通过原文校验即可引用，仍可随时停用。</p></div>
   <Radio.Group aria-label="新闻事实审核方式" value={form.review_mode} disabled={save.isPending} onChange={event=>change({review_mode:event.target.value})} optionType="button" options={[{value:'manual',label:'人工审核'},{value:'auto',label:'自动生效'}]}/></div>
  <div className="ai-review-fields">
   <div><label>从新闻自动提取事实</label><Space><Switch aria-label="从新闻自动提取事实" checked={form.extract_enabled} disabled={save.isPending} onChange={value=>change({extract_enabled:value})}/><span>{form.extract_enabled?'开启':'关闭'}</span></Space><p className="muted">每 5 分钟最多处理 3 篇与公投相关的新报道；每条事实都须在原文中找到逐字引句，数字须与引句一致。</p></div>
   <div><label htmlFor="fact-provider">提取模型连接</label><Select id="fact-provider" aria-label="提取模型连接" value={form.provider_id} disabled={save.isPending} onChange={value=>change({provider_id:value})} options={[{value:'',label:'请选择文字 AI 连接'},...data.providers.map(p=>({value:p.id,label:`${p.name}${p.model?` · ${p.model}`:''}${available(p)?'':' · 不可用'}`,disabled:!available(p)}))]}/><p className="muted">复用 <Link to="/ai/settings">AI 设置</Link> 中的文字连接。</p></div>
  </div>
  {invalid&&<Alert type="warning" showIcon title="开启提取前，请先选择可用的文字 AI 连接。"/>}
  {form.review_mode==='auto'&&baseline.review_mode==='manual'&&<Alert type="info" showIcon title="切换为自动生效后，已在待审中的事实不会自动生效，可在待审列表中一键全部通过。"/>}
  {conflict&&<Alert type="warning" showIcon title="设置已由其他窗口更新" action={<Button onClick={()=>{setBaseline(data.settings);setForm(editable(data.settings));}}>载入最新设置</Button>}/>}
  {error&&<Alert type="error" showIcon title="事实库设置未保存" description={error}/>}
  {saved&&!dirty&&<p role="status" className="ai-review-success">事实库设置已保存并生效。</p>}
  <div className="ai-review-save"><p className="muted">固定层事实来自官方设定，导入后直接生效；新闻层事实按上方方式审核。</p><Space wrap>{dirty&&<Tag color="orange">有未保存更改</Tag>}<Button type="primary" loading={save.isPending} disabled={!dirty||invalid||conflict} onClick={()=>void persist()}>保存设置</Button></Space></div>
 </section>;
}

function FactEditor({fact,concerns,topics,open,onClose,onSubmit,busy,title}:{fact?:Row;concerns:string[];topics:string[];open:boolean;onClose:()=>void;onSubmit:(values:Row)=>void;busy:boolean;title:string}){
 const [values,setValues]=useState<Row>({});
 useEffect(()=>{if(open)setValues({claim:fact?.claim||'',concerns:fact?.concerns||[],topic_key:fact?.topic_key||'',valid_until:fact?.valid_until||'',source_label:fact?.source_label||''});},[open,fact]);
 const set=(v:Row)=>setValues(current=>({...current,...v}));
 return <Modal open={open} title={title} onCancel={onClose} confirmLoading={busy} okText="保存" cancelText="取消" okButtonProps={{disabled:!values.claim?.trim()||!values.concerns?.length||(!fact&&!values.source_label?.trim())}} onOk={()=>onSubmit({...values,topic_key:values.topic_key||null,valid_until:values.valid_until||null})}>
  <div className="stack">
   <div><label>事实内容（英文，Agent 直接引用，最多 220 字符）</label><Input.TextArea aria-label="事实内容" maxLength={220} showCount autoSize={{minRows:2}} value={values.claim} onChange={e=>set({claim:e.target.value})}/></div>
   <div><label>对应顾虑</label><Select mode="multiple" aria-label="对应顾虑" style={{width:'100%'}} maxCount={6} value={values.concerns} onChange={v=>set({concerns:v})} options={concerns.map(c=>({value:c,label:label(c)}))}/></div>
   {!fact&&<div><label>来源</label><Input aria-label="来源" maxLength={200} value={values.source_label} onChange={e=>set({source_label:e.target.value})}/></div>}
   <div><label>时效主题（可选，同主题只保留最新一条）</label><Select aria-label="时效主题" allowClear style={{width:'100%'}} value={values.topic_key||undefined} onChange={v=>set({topic_key:v||''})} options={topics.map(t=>({value:t,label:t}))}/></div>
   <div><label>有效期至（可选，ISO 时间，如 2026-10-30T00:00:00Z）</label><Input aria-label="有效期至" value={values.valid_until} onChange={e=>set({valid_until:e.target.value})}/></div>
  </div>
 </Modal>;
}

function Facts({overview,status,layer}:{overview:Overview;status?:string;layer?:string}){
 const [q,setQ]=useState(''),[offset,setOffset]=useState(0),[editing,setEditing]=useState<Row|undefined>(),[mode,setMode]=useState<'edit'|'approve'|'create'|null>(null),[error,setError]=useState('');
 const list=useApi<{items:Row[];total:number;counts:Row}>('/agents/facts/list',{status,layer,q,limit:20,offset},30000,true);
 const decide=useAction('/agents/facts/decision'),create=useAction('/agents/facts/create'),approveAll=useAction('/agents/facts/approve-pending'),client=useQueryClient();
 const refresh=()=>void client.invalidateQueries({queryKey:['/agents/facts/list']});
 async function act(fact:Row,action:string,edits?:Row){
  setError('');
  try{await decide.mutateAsync({id:fact.id,action,expected_revision:fact.revision,...(edits?{edits}:{})});setMode(null);}
  catch(cause){setError(message(cause));}
  refresh();
 }
 async function add(values:Row){setError('');try{await create.mutateAsync(values);setMode(null);}catch(cause){setError(message(cause));}refresh();}
 const busy=decide.isPending||create.isPending;
 return <div className="stack">
  <Space wrap><Input.Search aria-label="搜索事实" allowClear placeholder="搜索事实内容或来源" onSearch={value=>{setQ(value);setOffset(0);}} style={{width:280}}/>
   {status==='pending'&&<Button disabled={!list.data?.total} loading={approveAll.isPending} onClick={()=>Modal.confirm({title:`全部通过 ${list.data?.total||0} 条待审事实？`,okText:'全部通过',cancelText:'取消',onOk:async()=>{try{await approveAll.mutateAsync({});}catch(cause){setError(message(cause));}refresh();}})}>全部通过</Button>}
   {status==='active'&&<Button onClick={()=>{setEditing(undefined);setMode('create');}}>手动添加事实</Button>}</Space>
  {error&&<Alert type="error" showIcon title="操作未完成" description={error} closable onClose={()=>setError('')}/>}
  {list.isError&&<Alert type="error" showIcon title="事实列表读取失败" description={list.error.message}/>}
  <Table<Row> size="middle" rowKey="id" loading={list.isPending} dataSource={list.data?.items||[]} scroll={{x:900}}
   pagination={{current:offset/20+1,pageSize:20,total:list.data?.total||0,showSizeChanger:false,onChange:page=>setOffset((page-1)*20)}}
   locale={{emptyText:<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无事实"/>}}
   expandable={{rowExpandable:row=>!!row.evidence_quote||!!row.decision_note,expandedRowRender:row=><div className="ai-review-record-detail">{row.evidence_quote&&<><strong>原文引句</strong><blockquote>{row.evidence_quote}</blockquote></>}{row.decision_note&&<p>处理备注：{row.decision_note}</p>}</div>}}
   columns={[
    {title:'事实',key:'claim',render:(_,row)=><><span className="ai-review-excerpt">{row.claim}</span><small className="ai-review-table-small">{row.layer==='core'?'固定层':'新闻层'}{row.topic_key?` · ${row.topic_key}`:''}{row.valid_until?` · 有效期至 ${time(row.valid_until)}`:''}</small></>},
    {title:'顾虑',key:'concerns',render:(_,row)=><Space size={[4,4]} wrap>{(row.concerns||[]).map((c:string)=><Tag key={c}>{label(c)}</Tag>)}</Space>},
    {title:'来源',key:'source',render:(_,row)=><><span className="ai-review-excerpt">{row.source_label}</span><small className="ai-review-table-small">{time(row.observed_at||row.created_at)}</small></>},
    {title:'状态',key:'status',render:(_,row)=><Tag color={statusColors[row.status]}>{statusLabels[row.status]||row.status}</Tag>},
    {title:'操作',key:'actions',render:(_,row)=><Space size={0} wrap>
     {['pending','rejected'].includes(row.status)&&<Button type="link" size="small" disabled={busy} onClick={()=>void act(row,'approve')}>通过</Button>}
     {row.status==='pending'&&<Button type="link" size="small" disabled={busy} onClick={()=>{setEditing(row);setMode('approve');}}>编辑后通过</Button>}
     {row.status==='pending'&&<Button type="link" size="small" danger disabled={busy} onClick={()=>void act(row,'reject')}>拒绝</Button>}
     {row.status==='active'&&<Button type="link" size="small" disabled={busy} onClick={()=>{setEditing(row);setMode('edit');}}>编辑</Button>}
     {row.status==='active'&&<Button type="link" size="small" danger disabled={busy} onClick={()=>void act(row,'retire')}>停用</Button>}
     {['retired','superseded'].includes(row.status)&&<Button type="link" size="small" disabled={busy} onClick={()=>void act(row,'restore')}>恢复</Button>}
    </Space>}
   ]}/>
  <FactEditor open={mode!==null} fact={mode==='create'?undefined:editing} concerns={overview.concerns} topics={overview.topics} busy={busy}
   title={mode==='create'?'手动添加事实（固定层，立即生效）':mode==='approve'?'编辑后通过':'编辑事实'} onClose={()=>setMode(null)}
   onSubmit={values=>{if(mode==='create')void add(values);else if(editing)void act(editing,mode==='approve'?'approve':'edit',{claim:values.claim,concerns:values.concerns,topic_key:values.topic_key,valid_until:values.valid_until});}}/>
 </div>;
}

function Rules({overview}:{overview:Overview}){
 const rules=useApi<{rebuttals:Row[];guardrails:Row[]}>('/agents/facts/rules',{},60000);
 const saveRebuttal=useAction('/agents/facts/rebuttal'),saveGuardrail=useAction('/agents/facts/guardrail'),client=useQueryClient();
 const [rebuttal,setRebuttal]=useState<Row|null>(null),[guardrail,setGuardrail]=useState<Row|null>(null),[error,setError]=useState('');
 const refresh=()=>void client.invalidateQueries({queryKey:['/agents/facts/rules']});
 async function submit(action:ReturnType<typeof useAction>,payload:Row,close:()=>void){setError('');try{await action.mutateAsync(payload);close();}catch(cause){setError(message(cause));}refresh();}
 if(rules.isPending)return <Skeleton active/>;
 return <div className="stack">
  {error&&<Alert type="error" showIcon title="保存未完成" description={error} closable onClose={()=>setError('')}/>}
  <h3>No 方论点与反驳</h3>
  <Table<Row> size="middle" rowKey="concern" pagination={false} dataSource={rules.data?.rebuttals||[]} columns={[
   {title:'顾虑',key:'concern',render:(_,row)=><Tag>{label(row.concern)}</Tag>},
   {title:'No 方论点',dataIndex:'no_argument'},{title:'我方回应',dataIndex:'line'},
   {title:'关联事实',key:'facts',render:(_,row)=>(row.fact_ids||[]).join(', ')},
   {title:'状态',key:'status',render:(_,row)=>row.status==='active'?<Tag color="green">生效</Tag>:<Tag>停用</Tag>},
   {title:'',key:'edit',render:(_,row)=><Button type="link" size="small" onClick={()=>setRebuttal({...row})}>编辑</Button>}]}/>
  <Button onClick={()=>setRebuttal({concern:'',no_argument:'',line:'',fact_ids:[],status:'active'})}>添加反驳</Button>
  <h3>禁止的说法</h3>
  <Table<Row> size="middle" rowKey="id" pagination={false} dataSource={rules.data?.guardrails||[]} columns={[
   {title:'规则',dataIndex:'rule'},{title:'状态',key:'status',render:(_,row)=>row.status==='active'?<Tag color="green">生效</Tag>:<Tag>停用</Tag>},
   {title:'',key:'edit',render:(_,row)=><Button type="link" size="small" onClick={()=>setGuardrail({...row})}>编辑</Button>}]}/>
  <Button onClick={()=>setGuardrail({rule:'',status:'active'})}>添加禁止说法</Button>
  <Modal open={!!rebuttal} title="编辑反驳" okText="保存" cancelText="取消" confirmLoading={saveRebuttal.isPending} onCancel={()=>setRebuttal(null)}
   onOk={()=>rebuttal&&void submit(saveRebuttal,{concern:rebuttal.concern,no_argument:rebuttal.no_argument,line:rebuttal.line,fact_ids:rebuttal.fact_ids,status:rebuttal.status,expected_revision:rebuttal.revision??null},()=>setRebuttal(null))}>
   {rebuttal&&<div className="stack">
    <Select aria-label="顾虑" disabled={!!rebuttal.revision} value={rebuttal.concern||undefined} onChange={v=>setRebuttal({...rebuttal,concern:v})} options={overview.concerns.map(c=>({value:c,label:label(c)}))}/>
    <Input.TextArea aria-label="No 方论点" maxLength={400} value={rebuttal.no_argument} onChange={e=>setRebuttal({...rebuttal,no_argument:e.target.value})}/>
    <Input.TextArea aria-label="我方回应" maxLength={400} value={rebuttal.line} onChange={e=>setRebuttal({...rebuttal,line:e.target.value})}/>
    <Select aria-label="关联事实" mode="tags" maxCount={8} value={rebuttal.fact_ids} onChange={v=>setRebuttal({...rebuttal,fact_ids:v})}/>
    <Radio.Group value={rebuttal.status} onChange={e=>setRebuttal({...rebuttal,status:e.target.value})} options={[{value:'active',label:'生效'},{value:'retired',label:'停用'}]}/>
   </div>}
  </Modal>
  <Modal open={!!guardrail} title="编辑禁止说法" okText="保存" cancelText="取消" confirmLoading={saveGuardrail.isPending} onCancel={()=>setGuardrail(null)}
   onOk={()=>guardrail&&void submit(saveGuardrail,{id:guardrail.id??null,rule:guardrail.rule,status:guardrail.status,expected_revision:guardrail.revision??null},()=>setGuardrail(null))}>
   {guardrail&&<div className="stack">
    <Input.TextArea aria-label="禁止说法" maxLength={400} value={guardrail.rule} onChange={e=>setGuardrail({...guardrail,rule:e.target.value})}/>
    <Radio.Group value={guardrail.status} onChange={e=>setGuardrail({...guardrail,status:e.target.value})} options={[{value:'active',label:'生效'},{value:'retired',label:'停用'}]}/>
   </div>}
  </Modal>
 </div>;
}

type PromptRow = {key:string;label:string;applies_to:'both'|'staged'|'extraction'|'research';text:string;appendix?:{label:string;text:string}[]};
function FactGaps(){
 const query=useApi<{brief_id:string|null;generated_at:string|null;expired?:boolean;gaps:{concern:string;need:string}[]}>('/agents/facts/gaps',{},60000);
 if(query.isPending||query.isError||!Array.isArray(query.data?.gaps)||!query.data.gaps.length)return null;
 return <Alert type="info" showIcon title={`顾虑雷达建议补充的事实（${time(query.data.generated_at)}${query.data.expired?' · 报告已过期':''}）`}
  description={<ul className="fact-gaps">{query.data.gaps.map((gap,index)=><li key={index}><Tag>{label(gap.concern)}</Tag>{gap.need}</li>)}</ul>}/>;
}
const runLabels:Record<string,[string,string]>={done:['已提取','green'],skipped:['无可用事实','default'],failed:['失败','red']};
function Extraction({overview}:{overview:Overview}){
 const query=useApi<{items:Row[];totals:Row;interval_seconds:number;articles_per_run:number}>('/agents/facts/extractions',{limit:30},60000);
 const s=overview.settings,provider=overview.providers.find(p=>p.id===s.provider_id);
 return <div className="stack">
  <Space wrap>
   <Tag color={s.extract_enabled?'green':'default'}>{s.extract_enabled?'自动提取：开启':'自动提取：关闭'}</Tag>
   <Tag>{provider?`提取模型：${provider.name}${provider.model?` · ${provider.model}`:''}`:'未选择提取模型'}</Tag>
   <Tag color={s.review_mode==='auto'?'green':'blue'}>{s.review_mode==='auto'?'提取结果自动生效':'提取结果进入待审'}</Tag>
  </Space>
  <p className="muted">开关、模型连接和审核方式在页面上方的设置里修改。{query.data&&`每 ${Math.round(query.data.interval_seconds/60)} 分钟最多处理 ${query.data.articles_per_run} 篇新报道。累计 ${query.data.totals.runs} 篇，提取 ${query.data.totals.accepted} 条，原文校验丢弃 ${query.data.totals.dropped} 条，失败 ${query.data.totals.failed} 篇。`}</p>
  {query.isError&&<Alert type="error" showIcon title="提取记录读取失败" description={query.error.message}/>}
  <Table<Row> size="middle" rowKey={row=>`${row.article_key}:${row.updated_at}`} loading={query.isPending} dataSource={query.data?.items||[]} pagination={false} scroll={{x:800}}
   locale={{emptyText:<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有提取记录"/>}}
   columns={[
    {title:'报道',key:'title',render:(_,row)=><><span className="ai-review-excerpt">{row.title||row.article_key}</span><small className="ai-review-table-small">{row.site||''}{row.published_at?` · ${time(row.published_at)}`:''}</small></>},
    {title:'结果',key:'status',render:(_,row)=>{const [text,color]=runLabels[row.status]||[row.status,'default'];return <Tag color={color}>{text}</Tag>;}},
    {title:'提取 / 丢弃',key:'counts',render:(_,row)=>`${row.accepted} / ${row.dropped}`},
    {title:'处理时间',key:'updated',render:(_,row)=><>{time(row.updated_at)}{row.status==='failed'&&<small className="ai-review-table-small">第 {row.attempts} 次{row.next_attempt_at?`，${time(row.next_attempt_at)} 重试`:'，不再重试'}{row.error?`：${row.error}`:''}</small>}</>}]}/>
 </div>;
}
const appliesLabels:Record<string,string>={both:'两种流程都使用',staged:'仅分段流程',extraction:'仅新闻提取',research:'自动内容分析'};
function Prompts(){
 const query=useApi<{pipeline:string;prompts:PromptRow[];placeholders:Record<string,string>}>('/agents/facts/prompts',{},60000);
 if(query.isPending)return <Skeleton active/>;
 if(query.isError)return <Alert type="error" showIcon title="Prompt 读取失败" description={query.error.message}/>;
 const data=query.data;
 return <div className="stack">
  <p className="muted">只读：这里显示各步骤当前生效的系统指令，修改需要改代码。当前写作流程：<Tag>{data.pipeline==='staged'?'分段（决策→写作→检查）':'原流程'}</Tag>原流程的一次性指令写在代码里，切回原流程时使用，这里不显示。</p>
  <p className="muted">花括号是运行时替换的内容：{Object.entries(data.placeholders).map(([key,value])=><span key={key}><code>{key}</code> {value}；</span>)}</p>
  {data.prompts.map(prompt=><section key={prompt.key} aria-label={prompt.label}>
   <Space><h3 style={{margin:0}}>{prompt.label}</h3><Tag>{appliesLabels[prompt.applies_to]||prompt.applies_to}</Tag></Space>
   <pre className="fact-prompt">{prompt.text}</pre>
   {(prompt.appendix||[]).map(extra=><div key={extra.label}><small className="muted">{extra.label}</small><pre className="fact-prompt">{extra.text}</pre></div>)}
  </section>)}
 </div>;
}

const tabKeys=['pending','active','superseded','retired','rejected','rules','extraction','prompts'];
export default function FactBank(){
 const overview=useApi<Overview>('/agents/facts/settings'),counts=useApi<{counts:Row}>('/agents/facts/list',{limit:1},30000),client=useQueryClient();
 const c=counts.data?.counts||{};
 const [params,setParams]=useSearchParams(),tab=tabKeys.includes(params.get('tab')||'')?params.get('tab')!:'pending';
 const selectTab=(key:string)=>setParams(current=>{const next=new URLSearchParams(current);next.set('tab',key);return next;},{replace:true});
 return <div className="ai-review-page stack">
  <div className="ai-review-heading"><h2>事实库</h2>{overview.data&&<Tag color={overview.data.settings.review_mode==='auto'?'green':'blue'}>{overview.data.settings.review_mode==='auto'?'新闻事实：自动生效':'新闻事实：人工审核'}</Tag>}</div>
  {overview.isError&&<Alert type="error" showIcon title="事实库读取失败" description={overview.error.message} action={<Button onClick={()=>void overview.refetch()}>重试</Button>}/>}
  {overview.isPending?<Skeleton active paragraph={{rows:5}}/>:overview.data&&<>
   <Settings data={overview.data} onSaved={()=>void client.invalidateQueries({queryKey:['/agents/facts/settings']})}/>
   <FactGaps/>
   <section className="panel" aria-label="事实列表"><Tabs destroyOnHidden activeKey={tab} onChange={selectTab} items={[
    {key:'pending',label:`待审 ${c.pending||0}`,children:<Facts overview={overview.data} status="pending"/>},
    {key:'active',label:`生效 ${c.active||0}`,children:<Facts overview={overview.data} status="active"/>},
    {key:'superseded',label:`已被取代 ${c.superseded||0}`,children:<Facts overview={overview.data} status="superseded"/>},
    {key:'retired',label:`已停用 ${c.retired||0}`,children:<Facts overview={overview.data} status="retired"/>},
    {key:'rejected',label:`已拒绝 ${c.rejected||0}`,children:<Facts overview={overview.data} status="rejected"/>},
    {key:'rules',label:'反驳与禁止说法',children:<Rules overview={overview.data}/>},
    {key:'extraction',label:'事实提取',children:<Extraction overview={overview.data}/>},
    {key:'prompts',label:'Prompt（只读）',children:<Prompts/>}]}/></section>
   <p className="muted ai-review-footnote">Agent 写作只能引用"生效"的事实；同一时效主题只保留最新一条，过期的事实自动不再提供。</p>
  </>}
 </div>;
}
