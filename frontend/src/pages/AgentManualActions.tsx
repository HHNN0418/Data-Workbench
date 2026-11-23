import {useEffect,useRef,useState} from 'react';
import {Link} from 'react-router-dom';
import {useQueryClient} from '@tanstack/react-query';
import {Alert,Button,Input,InputNumber,Select,Space,Tag} from 'antd';
import {queryString,request,useAction,type Row} from '../api';
import {time} from '../common';

type ManualAction='post'|'reply'|'repost'|'like'|'unlike'|'follow'|'unfollow'|'delete_post';
type ManualAgent={id:string;name:string;username:string;account_id?:number|null;busy?:boolean;status?:string;generation?:{status?:string}};
type Prepared={draft_id:string;draft:Row;preview?:{actor?:Row;target_post?:Row;target_account?:Row}};
type InventoryKind='posts'|'likes'|'following';
const labels:Record<ManualAction,string>={post:'发帖',reply:'回复',repost:'转发',like:'点赞',unlike:'取消点赞',follow:'关注',unfollow:'取消关注',delete_post:'删除本人帖子'};
const postTargets=new Set<ManualAction>(['reply','repost','like','unlike','delete_post']);
const writtenActions=new Set<ManualAction>(['post','reply','repost']);
function validId(value:unknown):value is number{return typeof value==='number'&&Number.isSafeInteger(value)&&value>0;}
function draftStatus(draft?:Row){return draft?.status||draft?.state||'draft';}
const outcomeOrder:Record<string,number>={draft:0,pending:0,failed:1,sending:2,uncertain:3,published:4,discarded:4};
function rows(value:unknown):Row[]{return Array.isArray(value)?value:value&&typeof value==='object'?Object.values(value):[];}

export default function AgentManualActions({agent,drafts=[],sdkAvailable=true}:{agent:ManualAgent;drafts?:Row[];sdkAvailable?:boolean}){
 const client=useQueryClient(),prepareAction=useAction<Prepared>('/agents/manual/prepare'),publishAction=useAction<Row>('/agents/publish'),discardAction=useAction<Row>('/agents/discard');
 const [action,setAction]=useState<ManualAction>('post'),[postId,setPostId]=useState<number|null>(null),[accountId,setAccountId]=useState<number|null>(null),[body,setBody]=useState(''),[prepared,setPrepared]=useState<Prepared|null>(null),[confirmation,setConfirmation]=useState<number|null>(null),[error,setError]=useState(''),[notice,setNotice]=useState('');
 const locked=useRef(false),identity=useRef<{fingerprint:string;key:string}|null>(null),session=useRef(0);
 const [inventoryKind,setInventoryKind]=useState<InventoryKind>('posts'),[inventory,setInventory]=useState<Row|null>(null),[inventoryLoading,setInventoryLoading]=useState(false),[inventoryError,setInventoryError]=useState(''),inventoryRequest=useRef(0),inventoryLocked=useRef(false);
 useEffect(()=>{session.current++;setPrepared(null);setConfirmation(null);setError('');setNotice('');identity.current=null;},[agent.id]);
 useEffect(()=>{inventoryRequest.current++;setInventory(null);setInventoryError('');setInventoryLoading(false);inventoryLocked.current=false;},[agent.id,agent.account_id]);
 const roleRunning=agent.busy||['queued','running','cancelling'].includes(agent.generation?.status||'')||['running','reading','generating','publishing'].includes(agent.status||'');
 const busy=prepareAction.isPending||publishAction.isPending||discardAction.isPending,needsPost=postTargets.has(action),needsAccount=action==='follow'||action==='unfollow',written=writtenActions.has(action),needsBody=action==='post'||action==='reply';
 const latest=prepared?drafts.find(draft=>draft.id===prepared.draft_id):undefined,draft=prepared?{...prepared.draft,...latest}:undefined;
 // A confirmed receipt must not become clickable again while the shared poll
 // still contains the older pending draft.
 const localStatus=draftStatus(prepared?.draft),liveStatus=draftStatus(latest),status=(outcomeOrder[localStatus]??0)>(outcomeOrder[liveStatus]??0)?localStatus:liveStatus,pending=!!prepared&&['draft','pending','failed'].includes(status);
 const preview=prepared?.preview||prepared?.draft.manual_preview,actor=preview?.actor,targetPost=preview?.target_post,targetAccount=preview?.target_account;
 const expectedAccount=draft?.account_id??actor?.account_id,identityMatches=!!agent.account_id&&expectedAccount===agent.account_id&&draft?.agent_id===agent.id;
 const targetPreviewReady=!prepared?false:postTargets.has(draft!.action)?validId(targetPost?.post_id)&&validId(targetPost?.author_id)&&targetPost.post_id===draft!.target_post_id:['follow','unfollow'].includes(draft!.action)?validId(targetAccount?.account_id)&&targetAccount.account_id===draft!.target_account_id:true;
 const deletion=draft?.action==='delete_post',deletionOwner=!deletion||targetPost?.author_id===agent.account_id,deletionConfirmed=!deletion||confirmation===draft?.target_post_id;
 const canPrepare=!busy&&!prepared&&!!agent.account_id&&sdkAvailable&&(!needsPost||validId(postId))&&(!needsAccount||validId(accountId))&&(!needsBody||!!body.trim())&&(!written||body.length<=500);
 const preparedBody=String(draft?.body||''),preparedBodyValid=!writtenActions.has(draft?.action)||(preparedBody.length<=500&&(!['post','reply'].includes(draft?.action)||!!preparedBody.trim()));
 const canExecute=pending&&!busy&&identityMatches&&targetPreviewReady&&deletionOwner&&deletionConfirmed&&preparedBodyValid;
 const inventoryValid=inventory?.scope==='bound_account'&&inventory.agent_id===agent.id&&inventory.account_id===agent.account_id&&inventory.kind===inventoryKind;
 const inventoryRows=inventoryValid?rows(inventory?.items).filter(item=>inventoryKind==='following'?validId(item.account_id):validId(item.post_id)&&(inventoryKind!=='posts'||item.author_id===agent.account_id)):[];
 async function readInventory(next=false){
  if(inventoryLocked.current||busy||!agent.account_id||!sdkAvailable)return;
  inventoryLocked.current=true;setInventoryLoading(true);setInventoryError('');const sequence=++inventoryRequest.current;
  try{const result=await request<Row>(`/agents/${encodeURIComponent(agent.id)}/social?${queryString({kind:inventoryKind,limit:20,...(next&&inventoryValid&&inventory?.next_cursor?{cursor:inventory.next_cursor}:{})})}`);if(sequence!==inventoryRequest.current)return;if(result.scope!=='bound_account'||result.agent_id!==agent.id||result.account_id!==agent.account_id||result.kind!==inventoryKind){setInventory(null);throw new Error('服务器未返回当前绑定账号的资料，未显示结果。');}setInventory(result);}
  catch(cause){if(sequence===inventoryRequest.current)setInventoryError(cause instanceof Error?cause.message:'账号资料读取失败，请重试。');}
  finally{if(sequence===inventoryRequest.current){inventoryLocked.current=false;setInventoryLoading(false);}}
 }
 function selectInventory(item:Row,nextAction:ManualAction){
  if(prepared||busy||!inventoryValid)return;
  setAction(nextAction);setPostId(validId(item.post_id)?item.post_id:null);setAccountId(validId(item.account_id)?item.account_id:null);setBody('');setConfirmation(null);setError('');setNotice('已选择目标，请检查表单并预览操作。');
 }
 function updateCache(result:Row){if(result.agents&&result.settings)client.setQueryData(['/agents',{}],result);void client.invalidateQueries({queryKey:['/agents']});}
 async function prepare(){
  if(locked.current||!canPrepare)return;locked.current=true;setError('');setNotice('');const currentSession=session.current;
  const payload={agent_id:agent.id,action,body:written?body:'',...(needsPost?{target_post_id:postId}:{}),...(needsAccount?{target_account_id:accountId}:{})};
  const fingerprint=JSON.stringify(payload);if(identity.current?.fingerprint!==fingerprint)identity.current={fingerprint,key:crypto.randomUUID()};
  try{const result=await prepareAction.mutateAsync({...payload,idempotency_key:identity.current!.key});if(currentSession!==session.current)return;if(!result.draft_id||!result.draft)throw new Error('服务器未返回可审阅的手动操作，请重试准备操作。');setPrepared(result);setConfirmation(null);}
  catch(cause){if(currentSession===session.current)setError(cause instanceof Error?cause.message:'准备操作失败，请稍后重试。');}
  finally{locked.current=false;}
 }
 async function execute(){
  if(locked.current||!prepared||!canExecute)return;locked.current=true;setError('');setNotice('');const currentSession=session.current;
  try{const result=await publishAction.mutateAsync({draft_id:prepared.draft_id,body:writtenActions.has(draft!.action)?draft!.body||'':'',...(deletion?{confirmation_post_id:confirmation}:{})});updateCache(result);if(currentSession!==session.current)return;const updated=rows(result.drafts).find(item=>item.id===prepared.draft_id)||result.draft||{state:'sending',status:'sending'};setPrepared({...prepared,draft:{...prepared.draft,...updated}});setNotice(draftStatus(updated)==='uncertain'?'执行结果尚未确认，请到操作记录核对。':'执行结果已更新，可在操作记录与个人日志查看。');}
  catch(cause){if(currentSession===session.current)setError(cause instanceof Error?cause.message:'执行未能完成，请到操作记录核对。');void client.invalidateQueries({queryKey:['/agents']});}
  finally{locked.current=false;}
 }
 async function revise(){
  if(locked.current||!prepared||!pending)return;locked.current=true;setError('');const currentSession=session.current;
  try{const result=await discardAction.mutateAsync({draft_id:prepared.draft_id});updateCache(result);if(currentSession!==session.current)return;setPrepared(null);setConfirmation(null);identity.current=null;setNotice('旧手动草稿已放弃，可以修改并重新预览。');}
  catch(cause){if(currentSession===session.current)setError(cause instanceof Error?cause.message:'放弃操作失败，请稍后重试。');}
  finally{locked.current=false;}
 }
 return <section className="agents-manual stack" aria-label={`${agent.name}的手动操作`}>
  <div className="agents-section-head"><div><h3>手动管理账号操作</h3><p className="muted">选择目标并预览后，由你明确执行。这里不调用模型，手动草稿不会交给自动调度发布；手动纠正不受角色日限或反向冷却限制，仍遵守平台限流。</p></div><Link to={`/ai/agents/${encodeURIComponent(agent.id)}?tab=drafts`}>查看操作记录 ↗</Link></div>
  <div className="agents-manual-actor"><strong>{agent.name}</strong><span>@{agent.username}</span>{agent.account_id?<Link to={`/accounts?q=${agent.account_id}`}>当前操作账号 #{agent.account_id} ↗</Link>:<span>尚未连接账号</span>}</div>
  {roleRunning&&<Alert type="info" showIcon title="角色正在运行" description={<span>可先到 <Link to={`/ai/agents/${encodeURIComponent(agent.id)}?tab=overview`}>总览</Link> 等待当前操作完成，或取消仍在生成的决策后再手动操作。</span>}/>}
  {!agent.account_id&&<Alert type="info" showIcon title="先连接此角色的账号" description="连接后可以准备手动操作。"/>}{!sdkAvailable&&<Alert type="info" title="账号操作服务暂不可用" description="可保留输入内容，服务恢复后再准备操作。"/>}
  {error&&<Alert showIcon type="error" title={error}/>} {notice&&<Alert type="info" title={notice}/>}
  <section className="agents-manual-inventory stack" aria-label="当前账号社交资料"><div className="agents-section-head"><div><h4>账号现状</h4><p className="muted">按需读取当前账号的帖子、已点赞内容和关注列表。列表中的按钮只选择目标。</p></div><Space wrap><Select aria-label="账号现状类型" value={inventoryKind} disabled={busy||inventoryLoading} options={[{value:'posts',label:'本人帖子'},{value:'likes',label:'已点赞帖子'},{value:'following',label:'关注列表'}]} onChange={value=>{inventoryRequest.current++;setInventoryKind(value);setInventory(null);setInventoryError('');}}/><Button loading={inventoryLoading} disabled={busy||inventoryLoading||!agent.account_id||!sdkAvailable} onClick={()=>void readInventory()}>读取账号现状</Button></Space></div>
   {inventoryError&&<Alert type="error" showIcon title={inventoryError}/>} {!inventory&&!inventoryLoading&&!inventoryError&&<p className="muted">点击读取后显示平台当前返回的资料，不会自动轮询。</p>}
   {inventoryValid&&<><div className="agents-section-head"><span className="muted">本页 {inventoryRows.length} 条 · 读取时间 {time(inventory?.observed_at)}</span>{inventory?.next_cursor&&<Button disabled={busy||inventoryLoading} onClick={()=>void readInventory(true)}>下一页账号资料</Button>}</div>{prepared&&<p className="agents-draft-note">已有待确认预览。先返回修改并放弃旧草稿，再选择另一个目标。</p>}<div className="agents-manual-inventory-list">{!inventoryRows.length?<p className="muted">平台本页没有返回可显示的记录。</p>:inventoryRows.map(item=><article key={inventoryKind==='following'?`account-${item.account_id}`:`post-${item.post_id}`}><div className="agents-section-head"><Space wrap>{inventoryKind==='following'?<Link to={`/accounts?q=${item.account_id}`}>{item.username?`@${item.username}`:`账号 #${item.account_id}`} ↗</Link>:<><Link to={`/posts?post=${item.post_id}`}>帖子 #{item.post_id} ↗</Link>{item.author_id&&<Link to={`/accounts?q=${item.author_id}`}>作者 #{item.author_id} ↗</Link>}</>}</Space><Space wrap>{inventoryKind==='posts'?<><Button size="small" disabled={busy||!!prepared} aria-label={`选择回复帖子 #${item.post_id}`} onClick={()=>selectInventory(item,'reply')}>选择回复</Button><Button size="small" danger disabled={busy||!!prepared} aria-label={`选择删除帖子 #${item.post_id}`} onClick={()=>selectInventory(item,'delete_post')}>选择删除</Button></>:inventoryKind==='likes'?<Button size="small" disabled={busy||!!prepared} aria-label={`选择取消点赞帖子 #${item.post_id}`} onClick={()=>selectInventory(item,'unlike')}>选择取消点赞</Button>:<Button size="small" disabled={busy||!!prepared} aria-label={`选择取消关注账号 #${item.account_id}`} onClick={()=>selectInventory(item,'unfollow')}>选择取消关注</Button>}</Space></div>{inventoryKind==='following'?item.display_name&&<p>{item.display_name}</p>:<p className="agents-prose">{item.body||'此帖子没有文字正文。'}</p>}</article>)}</div></>}
  </section>
  <div className="agents-manual-form"><label><span>手动动作</span><Select aria-label="手动动作" value={action} disabled={busy||!!prepared} options={Object.entries(labels).map(([value,label])=>({value,label}))} onChange={value=>{setAction(value);setConfirmation(null);setError('');setNotice('');}}/></label>
   {needsPost&&<label><span>目标帖子 ID</span><InputNumber aria-label="目标帖子 ID" min={1} precision={0} value={postId} disabled={busy||!!prepared} onChange={value=>setPostId(typeof value==='number'?value:null)}/></label>}
   {needsAccount&&<label><span>目标账号 ID</span><InputNumber aria-label="目标账号 ID" min={1} precision={0} value={accountId} disabled={busy||!!prepared} onChange={value=>setAccountId(typeof value==='number'?value:null)}/></label>}
  </div>
  {written&&<label className="agents-manual-body"><span>{action==='repost'?'转发附言（可选）':'正文'}</span><Input.TextArea aria-label="手动操作正文" value={body} disabled={busy||!!prepared} onChange={event=>setBody(event.target.value)} autoSize={{minRows:4,maxRows:12}} maxLength={500} showCount placeholder="按角色平时的语气填写，避免破折号、模板口号和花哨符号。"/></label>}
  {!prepared&&<Space><Button type="primary" loading={prepareAction.isPending} disabled={!canPrepare} onClick={()=>void prepare()}>预览并准备操作</Button><span className="muted">此步骤只保存待审草稿，不执行外部操作。</span></Space>}
  {prepared&&<section className="agents-manual-preview stack" aria-label="手动操作执行预览"><div className="agents-section-head"><h4>执行前预览</h4><Space><Tag>{labels[draft!.action as ManualAction]||draft!.action}</Tag><Tag>{({draft:'待确认',pending:'待确认',failed:'未完成',sending:'执行中',published:'已完成',uncertain:'待核对',discarded:'已放弃'} as Record<string,string>)[status]||status}</Tag></Space></div>
   <p>将使用 <strong>{actor?.username?`@${actor.username}`:agent.name}</strong> 的账号 <strong>#{expectedAccount??'待确认'}</strong> 执行。</p>
   {targetPost&&<article><Space wrap><strong>目标帖子 #{targetPost.post_id}</strong><Link to={`/posts?post=${targetPost.post_id}`}>查看帖子 ↗</Link><Link to={`/accounts?q=${targetPost.author_id}`}>作者 {targetPost.username?`@${targetPost.username}`:`#${targetPost.author_id}`} ↗</Link></Space><p className="agents-prose">{targetPost.body||'此帖子没有文字正文。'}</p></article>}
   {targetAccount&&<article><Space wrap><strong>目标账号 {targetAccount.username?`@${targetAccount.username}`:`#${targetAccount.account_id}`}</strong><Link to={`/accounts?q=${targetAccount.account_id}`}>账号 #{targetAccount.account_id} ↗</Link></Space>{targetAccount.display_name&&<p>{targetAccount.display_name}</p>}</article>}
   {writtenActions.has(draft!.action)&&<article><h4>即将发布的正文</h4><p className="agents-prose">{draft!.body||(draft!.action==='repost'?'直接转发，不附加正文。':'正文为空，请返回修改。')}</p></article>}
   {!identityMatches&&<Alert type="warning" showIcon title="操作账号与当前绑定不一致，请重新准备"/>}{!targetPreviewReady&&<Alert type="warning" showIcon title="未收到完整目标预览，请到操作记录查看或重新准备"/>}
   {deletion&&<div className="agents-manual-delete"><strong>删除后无法恢复。</strong><p>仅可删除此角色本人发布的帖子。核对上面的作者与正文，输入帖子 ID {draft!.target_post_id} 确认删除。</p>{!deletionOwner&&<Alert type="error" title="目标作者不是此角色当前账号，不能删除"/>}<InputNumber aria-label="输入帖子 ID 确认删除" value={confirmation} min={1} precision={0} disabled={busy||!pending} onChange={value=>setConfirmation(typeof value==='number'?value:null)}/></div>}
   {status==='uncertain'&&<Alert type="warning" showIcon title="结果尚未确认，不能重复执行" description={<Link to={`/ai/agents/${encodeURIComponent(agent.id)}?tab=drafts&draft=${encodeURIComponent(prepared.draft_id)}`}>到操作记录核对远端结果</Link>}/>}
   <Space wrap>{['published','discarded'].includes(status)&&<Button disabled={busy} onClick={()=>{setPrepared(null);setConfirmation(null);setBody('');setPostId(null);setAccountId(null);setNotice('');identity.current=null;}}>开始新的手动操作</Button>}{pending&&<Button disabled={busy} onClick={()=>void revise()}>返回修改并放弃旧草稿</Button>}{pending&&<Button type="primary" danger={deletion} loading={publishAction.isPending} disabled={!canExecute} onClick={()=>void execute()}>{deletion?'永久删除这条帖子':`确认执行${labels[draft!.action as ManualAction]||'操作'}`}</Button>}<Link to={`/ai/agents/${encodeURIComponent(agent.id)}?tab=drafts&draft=${encodeURIComponent(prepared.draft_id)}`}>查看此操作与结果 ↗</Link></Space>
  </section>}
 </section>;
}
