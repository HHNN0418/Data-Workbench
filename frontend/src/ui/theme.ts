// The only place colours are defined (docs/UX_IA_PLAN.md §13.1): the antd theme reads them here and
// applyCssVariables() exposes them to stylesheets as --brand, --text-2, … before the first render.
export const colors={
 brand:'#26705b',brandWeak:'#e8f1ec',
 // text3 stays at about 4.5:1 on white so small helper text remains readable.
 text:'#252a28',text2:'#5f675f',text3:'#747b76',
 border:'#dfe3dd',borderWeak:'#edf0e8',
 bg:'#fafaf8',bgPanel:'#ffffff',bgSunken:'#f6f7f3',
 ok:'#2f855a',warn:'#b7791f',danger:'#c53030',info:'#2b6cb0',neutral:'#8a9184',
 stanceYes:'#2b6cb0',stanceNo:'#c05621',stanceUndecided:'#6b46c1',stanceUnrelated:'#8a9184',
 own:'#26705b',bot:'#975a16'
} as const;

export const cssVariables:Record<keyof typeof colors,string>={
 brand:'--brand',brandWeak:'--brand-weak',text:'--text',text2:'--text-2',text3:'--text-3',border:'--border',borderWeak:'--border-weak',
 bg:'--bg',bgPanel:'--bg-panel',bgSunken:'--bg-sunken',ok:'--ok',warn:'--warn',danger:'--danger',info:'--info',neutral:'--neutral',
 stanceYes:'--stance-yes',stanceNo:'--stance-no',stanceUndecided:'--stance-undecided',stanceUnrelated:'--stance-unrelated',own:'--own',bot:'--bot'
};

export function applyCssVariables(root:HTMLElement=document.documentElement) {
 for(const [key,name] of Object.entries(cssVariables))root.style.setProperty(name,colors[key as keyof typeof colors]);
}

export const antdTheme={
 token:{colorPrimary:colors.brand,colorInfo:colors.brand,colorSuccess:colors.ok,colorWarning:colors.warn,colorError:colors.danger,colorText:colors.text,colorTextSecondary:colors.text3,colorBgLayout:colors.bg,colorBorder:colors.border,borderRadius:6,fontFamily:'Inter, "Segoe UI", "Microsoft YaHei", sans-serif',fontSize:14,controlHeight:36},
 components:{Table:{headerBg:colors.bgSunken,headerColor:colors.text3,cellPaddingBlock:15},Button:{primaryShadow:'none'},Tabs:{horizontalItemGutter:28},Modal:{borderRadiusLG:12}}
};
