// Single source for the sidebar, breadcrumbs and page titles. The order follows the data flow
// (docs/PIPELINE_UI_SPEC.md §2): collect → process → analyse → strategy → agent execution → results.
export type GroupKey='overview'|'collect'|'process'|'analyze'|'strategy'|'execute'|'results'|'system';
export interface NavItem {key:string;group:GroupKey;label:string;to:string;description:string;external?:boolean;
 // false while the page still renders its own <h1>.
 shellTitle?:boolean}
export interface NavGroup {key:GroupKey;label:string;step?:string;collapsible?:boolean}
export interface Crumb {label:string;to?:string}

export const navGroups:NavGroup[]=[
 {key:'overview',label:''},
 {key:'collect',label:'采集',step:'①'},
 {key:'process',label:'处理',step:'②'},
 {key:'analyze',label:'分析',step:'③'},
 {key:'strategy',label:'策略',step:'④'},
 {key:'execute',label:'Agent 执行',step:'⑤'},
 {key:'results',label:'效果',step:'⑥'},
 {key:'system',label:'系统',step:'⚙',collapsible:true}
];

export const navItems:NavItem[]=[
 {key:'cockpit',group:'overview',label:'全链路总览',to:'/',description:'从采集到得分的整条链路'},
 {key:'operations',group:'collect',label:'采集运行',to:'/operations',description:'采集任务、代理、Feed 覆盖与延迟',shellTitle:false},
 {key:'posts',group:'collect',label:'帖子流',to:'/posts',description:'原始帖子，Latest 与 Trending'},
 {key:'accounts',group:'collect',label:'账号',to:'/accounts',description:'账号资料与本队登记'},
 {key:'news',group:'collect',label:'新闻与话题',to:'/news',description:'新闻文章、新闻账号与话题标签'},
 {key:'annotations',group:'process',label:'标注',to:'/processing',description:'立场、顾虑与线程深度'},
 {key:'bots',group:'process',label:'账号识别',to:'/processing/bots',description:'市民 NPC、机器人、高频号、我方与公众人物'},
 {key:'risk',group:'process',label:'风险评估',to:'/processing/risk',description:'所有账号被隐藏的帖子、我方封号风险与别队账号举报条件'},
 {key:'extraction',group:'process',label:'事实提取',to:'/ai/facts?tab=extraction',description:'从新闻提取可引用的事实'},
 {key:'insights',group:'analyze',label:'舆论与洞察',to:'/analysis',description:'立场走势、发帖时机与内容效果'},
 {key:'radar',group:'analyze',label:'顾虑雷达',to:'/ai/analysis',description:'雷达报告与研究'},
 {key:'clusters',group:'analyze',label:'账号簇 / 竞品',to:'/analysis/clusters',description:'按行为聚出的账号组，和我方对照'},
 {key:'leaderboard',group:'analyze',label:'排行榜',to:'/analysis/leaderboard',description:'所有队伍的得分、1/6/24 小时变化和每小时速度'},
 {key:'blue',group:'analyze',label:'Blue 候选',to:'/blue',description:'举报候选与账号筛查'},
 {key:'workbench',group:'analyze',label:'AI 工作台',to:'/ai/analysis?ai_view=workspace',description:'热门分析、生成文章与资料聊天'},
 {key:'facts',group:'strategy',label:'事实库',to:'/ai/facts',description:'可引用的事实与审核'},
 {key:'targeting',group:'strategy',label:'目标与配比规则',to:'/strategy',description:'可选动作、发帖与回复配比、线程上限'},
 {key:'prompts',group:'strategy',label:'提示词与模板',to:'/ai/facts?tab=prompts',description:'写作提示词（只读）'},
 {key:'reading',group:'strategy',label:'资料筛选',to:'/ai/reading',description:'每个角色阅读哪些资料'},
 {key:'pipeline',group:'execute',label:'流水线监控',to:'/agents/pipeline',description:'选读、决策、写作、检查与发布队列'},
 {key:'roles',group:'execute',label:'角色',to:'/ai/agents',description:'角色状态、配置与运行'},
 {key:'pending',group:'execute',label:'待处理',to:'/ai/agents?tab=inbox',description:'未确认的通知与待执行的操作'},
 {key:'review',group:'execute',label:'发布审核',to:'/ai/review',description:'发布前检查与审核记录'},
 {key:'quotas',group:'execute',label:'额度',to:'/ai/agents?tab=quotas',description:'平台额度与发文竞赛'},
 {key:'score',group:'results',label:'得分与目标',to:'/score',description:'官方得分、门槛速度与对账',shellTitle:false},
 {key:'attribution',group:'results',label:'效果归因',to:'/results/attribution',description:'哪些顾虑和角度带来互动与转变'},
 {key:'exposure',group:'results',label:'曝光与说服',to:'/results/exposure',description:'浏览、Trending 与向我方的转变'},
 {key:'ai-connections',group:'system',label:'AI 连接',to:'/ai/settings',description:'模型连接与密钥'},
 {key:'rules',group:'system',label:'规则与情景',to:'/rules',description:'计分规则与情景计算'},
 {key:'settings',group:'system',label:'设置中心',to:'/settings',description:'全部设置的索引'},
 {key:'maintenance',group:'system',label:'维护',to:'/system/maintenance',description:'维护状态、备份与数据保留'},
 {key:'docs',group:'system',label:'API 参考',to:'/docs',description:'后端接口文档',external:true}
];

const byKey=Object.fromEntries(navItems.map(item=>[item.key,item])) as Record<string,NavItem>;
const simplePaths:Record<string,string>={'/':'cockpit','/operations':'operations','/posts':'posts','/accounts':'accounts','/news':'news',
 '/processing':'annotations','/processing/bots':'bots','/processing/risk':'risk','/analysis':'insights','/analysis/clusters':'clusters','/analysis/leaderboard':'leaderboard','/blue':'blue','/strategy':'targeting','/ai/reading':'reading',
 '/agents/pipeline':'pipeline','/ai/review':'review','/score':'score','/results/attribution':'attribution','/results/exposure':'exposure',
 '/ai/settings':'ai-connections','/rules':'rules','/settings':'settings','/system/maintenance':'maintenance'};
const agentDetail=/^\/ai\/agents\/([^/]+)\/?$/;

function params(search:string|URLSearchParams) {return typeof search==='string'?new URLSearchParams(search):search;}

// The sidebar item for a location; query parameters decide between items sharing a path.
export function matchNav(pathname:string,search:string|URLSearchParams=''):NavItem|undefined {
 const query=params(search),path=pathname.length>1?pathname.replace(/\/+$/,''):pathname;
 if(path==='/ai/analysis'){const view=query.get('ai_view');return byKey[view==='screening'?'blue':view==='workspace'||view==='chat'?'workbench':'radar'];}
 if(path==='/ai/facts'){const tab=query.get('tab');return byKey[tab==='extraction'?'extraction':tab==='prompts'?'prompts':'facts'];}
 if(path==='/ai/agents'){const tab=query.get('tab');return byKey[tab==='drafts'||tab==='inbox'?'pending':tab==='quotas'?'quotas':'roles'];}
 if(agentDetail.test(path))return byKey.roles;
 return simplePaths[path]?byKey[simplePaths[path]]:undefined;
}

// Labels of tabs that live in the URL; values not listed are not shown, so renamed tabs never show a stale label.
const teamTabs:Record<string,string>={roles:'角色',drafts:'操作记录',inbox:'收到的通知',logs:'运行记录',settings:'队伍设置'};
const detailTabs:Record<string,string>={overview:'总览',persona:'角色配置',memory:'经历记忆',drafts:'操作记录',manual:'手动操作',relationships:'社交关系',inbox:'收到的通知',logs:'个人日志'};
const pageTabs:Record<string,{param:string;labels:Record<string,string>}>={
 '/':{param:'view',labels:{data:'数据总览'}},
 '/posts':{param:'feed',labels:{latest:'Latest',trending:'Trending',all:'全部已采集'}},
 '/news':{param:'news_view',labels:{articles:'新闻文章',news_accounts:'新闻账号',tags:'话题标签'}},
 '/analysis':{param:'analysis_tab',labels:{trend:'趋势',cohorts:'账号对比',growth:'窗口增长',correlation:'相关性',keywords:'词频',network:'互动关系',quality:'数据质量',deep:'深度分析'}}
};

export function tabLabel(pathname:string,search:string|URLSearchParams=''):string|undefined {
 const query=params(search),tab=query.get('tab')||'';
 if(agentDetail.test(pathname))return detailTabs[tab];
 if(pathname==='/ai/agents')return teamTabs[tab];
 const page=pageTabs[pathname];
 return page?page.labels[query.get(page.param)||'']:undefined;
}

export function pageTitle(pathname:string,search:string|URLSearchParams=''):string|undefined {
 const item=matchNav(pathname,search);
 return item&&item.shellTitle!==false?item.label:undefined;
}

export function groupLabel(key:GroupKey):string|undefined {return navGroups.find(group=>group.key===key)?.label||undefined;}

// stage / item / entity / tab. The current page is the last crumb and has no link.
export function breadcrumbs(pathname:string,search:string|URLSearchParams='',agentName?:(id:string)=>string|undefined):Crumb[] {
 const query=params(search),item=matchNav(pathname,query);
 if(!item)return [];
 const group=groupLabel(item.group),crumbs:Crumb[]=[...(group?[{label:group}]:[]),{label:item.label,to:item.to}],page=crumbs.length-1;
 const detail=pathname.match(agentDetail),post=pathname==='/posts'?query.get('post'):null,tab=tabLabel(pathname,query);
 if(detail){const id=decodeURIComponent(detail[1]);crumbs.push({label:agentName?.(id)||id,to:pathname});}
 if(post){
  // The post drawer sits over the filtered list; going back keeps the filters.
  const list=new URLSearchParams(query);list.delete('post');
  crumbs[page]={...crumbs[page],to:`/posts${list.toString()?`?${list}`:''}`};crumbs.push({label:`#${post}`});
 }else if(tab&&tab!==item.label)crumbs.push({label:tab});
 return crumbs.map((crumb,index)=>index===crumbs.length-1?{label:crumb.label}:crumb);
}
