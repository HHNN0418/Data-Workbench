export type Provider = {id:string;name:string;base_url:string;model:string;protocol:string;has_key:boolean;enabled:boolean;is_default:boolean;token_parameter?:string;thinking_mode?:'auto'|'enabled'|'disabled';max_output_tokens?:number|null;competition_tier?:'standard'|'enhanced'|'premium';connection_timeout_seconds?:number;provider_order?:string[]};
export type Settings = {providers:Provider[];default_provider_id?:string|null};

export function localEndpoint(baseUrl:string):boolean {try{return ['localhost','127.0.0.1','[::1]','::1'].includes(new URL(baseUrl).hostname);}catch{return false;}}
export function openRouterEndpoint(baseUrl?:string):boolean {try{return new URL(baseUrl||'').hostname==='openrouter.ai';}catch{return false;}}
export function providerReady(provider?:Provider):boolean {return !!provider?.enabled&&(provider.has_key||localEndpoint(provider.base_url));}
