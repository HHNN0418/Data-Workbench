import {Fragment, useState} from 'react';
import {Link, useSearchParams} from 'react-router-dom';
import {Alert, Button, Form, Input, InputNumber, Modal, Tabs} from 'antd';
import {useApi, type Row} from '../api';
import {Metrics, QueryState, time} from '../common';
import {RelativeTime, StatusTag} from '../ui/status';
import PaceAlert from './PaceAlert';
import Overview from './Overview';
import {bottleneck, flowLabel, n, nodeView, num, stageLinks, stageOrder, stageReasons, stageSteps, stageTone, statusText, type StageKey} from './pipelineModel';
import './pipeline.css';

// The leaderboard lives on the competition portal, which is read-only for us (D22): the leader's score is
// copied by hand and kept in this browser only.
const boardKey='ctn.leaderboard.v1';
export interface BoardEntry {leader:number;name?:string;rank?:number;at:string}
export function readBoard():BoardEntry|null {
 try {const value=JSON.parse(window.localStorage.getItem(boardKey)||'null');return value&&num(value.leader)!==null?value:null;}
 catch {return null;}
}
function writeBoard(entry:BoardEntry) {try {window.localStorage.setItem(boardKey,JSON.stringify(entry));} catch {/* storage blocked: keep it for this visit only */}}

function BoardModal({entry,open,onClose,onSave}:{entry:BoardEntry|null;open:boolean;onClose:()=>void;onSave:(entry:BoardEntry)=>void}) {
 const [form]=Form.useForm();
 return <Modal title="录入排行榜" open={open} onCancel={onClose} onOk={()=>form.submit()} okText="保存" cancelText="取消" destroyOnHidden>
  <p className="muted">排行榜在竞赛门户上，只能手动抄录。只保存在这台浏览器里。</p>
  <Form form={form} layout="vertical" initialValues={entry||undefined} onFinish={values=>onSave({leader:Number(values.leader),name:values.name||undefined,rank:values.rank?Number(values.rank):undefined,at:new Date().toISOString()})}>
   <Form.Item name="leader" label="榜首分数" rules={[{required:true,message:'请输入榜首分数'}]}><InputNumber min={0} style={{width:'100%'}}/></Form.Item>
   <Form.Item name="name" label="榜首队名（可不填）"><Input/></Form.Item>
   <Form.Item name="rank" label="我们的排名（可不填）"><InputNumber min={1} precision={0} style={{width:'100%'}}/></Form.Item>
  </Form>
 </Modal>;
}

// Score, the poll pace against what is needed now, and the distance to the leader (PIPELINE_UI_SPEC §3 top row).
function TopRow({outcome,updatedAt}:{outcome:Row;updatedAt:unknown}) {
 const [board,setBoard]=useState<BoardEntry|null>(readBoard),[editing,setEditing]=useState(false);
 const total=num(outcome.total),gap=board&&total!==null?board.leader-total:null,poll=outcome.next_poll??'—';
 const edit=<Button type="link" size="small" className="pipeline-board-edit" onClick={()=>setEditing(true)}>{board?'修改':'录入榜首分数'}</Button>;
 return <>
  <Metrics items={[
   {label:'官方得分',value:n(outcome.total),hint:outcome.score_stale?'积分快照可能过期':<>快照 <RelativeTime value={updatedAt}/></>},
   {label:`Poll ${poll} 速度（6 小时）`,value:n(outcome.points_per_hour_6h),hint:`需要 ${n(outcome.required_points_per_hour)} 分/时 · 12 小时 ${n(outcome.points_per_hour_12h)} 分/时`},
   {label:`距 Poll ${poll} 门槛`,value:n(outcome.gap_to_threshold),hint:outcome.on_track===false?'按当前速度赶不上':outcome.on_track?'按当前速度赶得上':undefined},
   {label:'落后榜首',value:gap===null?null:gap>0?n(gap):gap===0?'持平':`领先 ${n(-gap)}`,hint:<>{board&&<>{board.name||'榜首'} {n(board.leader)} 分{board.rank?` · 我们第 ${board.rank} 名`:''} · 录入 <RelativeTime value={board.at}/> </>}{edit}</>}
  ]}/>
  <BoardModal entry={board} open={editing} onClose={()=>setEditing(false)} onSave={entry=>{writeBoard(entry);setBoard(entry);setEditing(false);}}/>
 </>;
}

function Bottleneck({stage,others,thresholds}:{stage:Row;others:Row[];thresholds:Row}) {
 const key=stage.key as StageKey,reasons=stageReasons(key,stage.metrics,thresholds);
 return <Alert type={stage.status==='bad'?'error':'warning'} showIcon title={`瓶颈在「${stage.label}」`} description={<div className="pipeline-why">
  <p>{stage.headline}</p>
  {reasons.length>0&&<ul>{reasons.map(reason=><li key={reason}>{reason}</li>)}</ul>}
  {others.length>0&&<p className="muted">另外需要看：{others.map(other=>`${stageSteps[other.key as StageKey]} ${other.label}（${statusText[other.status]}）`).join('、')}</p>}
 </div>} action={<Link to={stageLinks[key]}>查看{stage.label}</Link>}/>;
}

function StageNode({stage,isBottleneck}:{stage:Row;isBottleneck:boolean}) {
 const key=stage.key as StageKey,view=nodeView(key,stage.metrics),tone=stageTone(stage.status),detail=`pipeline-${key}-detail`,state=statusText[stage.status]||stage.status;
 return <Link to={stageLinks[key]} className={`pipeline-node pipeline-${tone}${isBottleneck?' pipeline-bottleneck':''}`} aria-label={`${stageSteps[key]} ${stage.label}：${state}`} aria-describedby={detail}>
  <div className="pipeline-node-head"><span className="pipeline-step">{stageSteps[key]}</span><strong>{stage.label}</strong><StatusTag tone={tone}>{state}</StatusTag></div>
  <div className="pipeline-node-body" id={detail}>
   {stage.status==='unknown'
    ?<p className="pipeline-headline">这一段读取失败{stage.error?`（${stage.error}）`:''}</p>
    :<>
     <div className="pipeline-main"><strong>{view.main}</strong><span>{view.mainLabel}</span></div>
     <ul className="pipeline-details">{view.details.map(text=><li key={text}>{text}</li>)}</ul>
     <p className="pipeline-headline">{stage.headline}</p>
    </>}
  </div>
  <span className="pipeline-updated">更新 <RelativeTime value={stage.updated_at}/></span>
 </Link>;
}

function PipelineFlow() {
 const pipeline=useApi<Row>('/pipeline/status',{},60000,true);
 return <QueryState query={pipeline}>{data=>{
  const stages=stageOrder.map(key=>(data.stages||[]).find((stage:Row)=>stage.key===key)).filter(Boolean) as Row[];
  const byKey=Object.fromEntries(stages.map(stage=>[stage.key,stage])) as Partial<Record<StageKey,Row>>;
  const outcome=byKey.outcome?.metrics||{},analyze=byKey.analyze?.metrics||{},worst=bottleneck(stages);
  const others=stages.filter(stage=>stage!==worst&&(stage.status==='bad'||stage.status==='warn'));
  return <div className="stack">
   <TopRow outcome={outcome} updatedAt={byKey.outcome?.updated_at}/>
   {worst&&<Bottleneck stage={worst} others={others} thresholds={data.thresholds||{}}/>}
   <div className="pipeline">
    <ol className="pipeline-flow" aria-label="全链路">
     {stages.map((stage,index)=><Fragment key={stage.key}>
      <li className="pipeline-cell"><StageNode stage={stage} isBottleneck={worst?.key===stage.key}/></li>
      {index<stages.length-1&&<li className="pipeline-arrow"><span className="pipeline-arrow-label"><span className="pipeline-sr">流向 {stages[index+1].label}：</span>{flowLabel(stage.key as StageKey,byKey)}</span></li>}
     </Fragment>)}
    </ol>
    {/* The results feed the strategy back: attribution re-ranks priorities, the radar picks concerns. */}
    <div className="pipeline-loop-row"><div className="pipeline-loop" role="group" aria-label="效果回流到策略">
     <span className="pipeline-loop-title">⑥ 效果回流到 ④ 策略</span>
     <Link to="/results/attribution">归因已结算 {n(outcome.attribution_settled)} / {n(outcome.attribution_total)}</Link>
     <Link to="/ai/analysis">{analyze.radar_generated_at?<>当前雷达生成于 {time(analyze.radar_generated_at)}</>:'还没有雷达'}</Link>
    </div></div>
   </div>
   <p className="muted">链路状态计算于 {time(data.generated_at)}，每分钟刷新；颜色和阈值由后端判定。</p>
  </div>;
 }}</QueryState>;
}

// Home: the whole chain from collection to score on one screen (docs/PIPELINE_UI_SPEC.md §3). The former
// cockpit (data size, coverage, latest posts) stays on the second tab.
export default function PipelineHome() {
 const [search,setSearch]=useSearchParams(),view=search.get('view')==='data'?'data':'flow';
 const overview=useApi<Row>('/overview',{},60000);
 return <div className="stack">
  <PaceAlert threshold={overview.data?.threshold}/>
  <Tabs activeKey={view} onChange={key=>setSearch(previous=>{const next=new URLSearchParams(previous);if(key==='data')next.set('view','data');else next.delete('view');return next;})} items={[
   {key:'flow',label:'全链路',children:<PipelineFlow/>},
   {key:'data',label:'数据总览',children:<Overview/>}
  ]}/>
 </div>;
}
