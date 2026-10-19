<#ftl output_format="plainText">
<#import "template.ftl" as layout>
<#assign exp = linkExpirationFormatter(linkExpiration)>
<@layout.emailLayout heading=msg("executeActionsHeading") link=link label=msg("executeActionsButton") note=msg("executeActionsNote", exp)>
<#if (user.firstName)?has_content>${msg("greetingName", user.firstName)}<#else>${msg("greeting")}</#if>

${msg("executeActionsLead")}
<#if requiredActions?? && requiredActions?has_content>

${msg("executeActionsWhat")}:
<#list requiredActions as reqActionItem>
- ${msg("requiredAction." + reqActionItem)}
</#list>
</#if>
</@layout.emailLayout>
