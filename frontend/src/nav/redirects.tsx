import {Navigate, useLocation} from 'react-router-dom';

// Old addresses keep working: the query and hash are kept and the old entry is replaced in history.
export function LegacyRedirect({to,dropAnalysisTab=false}:{to?:string;dropAnalysisTab?:boolean}) {
 const location=useLocation(),search=new URLSearchParams(location.search);
 if(dropAnalysisTab)search.delete('analysis_tab');
 const pathname=to??location.pathname.replace(/^\/agents(?=\/|$)/,'/ai/agents');
 return <Navigate replace to={{pathname,search:search.toString()?`?${search}`:'',hash:location.hash}}/>;
}
