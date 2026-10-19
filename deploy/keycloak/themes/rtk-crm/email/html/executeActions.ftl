<#import "template.ftl" as layout>
<#assign exp = linkExpirationFormatter(linkExpiration)>
<@layout.emailLayout heading=msg("executeActionsHeading") preheader=msg("executeActionsPreheader", exp) link=link label=msg("executeActionsButton") note=msg("executeActionsNote", exp)>
<p style="margin:0 0 12px 0;"><#if (user.firstName)?has_content>${msg("greetingName", user.firstName)}<#else>${msg("greeting")}</#if></p>
<p style="margin:0 0 12px 0;">${msg("executeActionsLead")}</p>
<#if requiredActions?? && requiredActions?has_content>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="bg-soft" style="background-color:#f9f9fa;border-radius:8px;margin:12px 0;">
<tr><td class="t-muted" style="padding:10px 16px;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:20px;color:#6d7686;width:38%;vertical-align:top;">${msg("executeActionsWhat")}</td><td class="t-main" style="padding:10px 16px;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:20px;color:#101828;font-weight:bold;vertical-align:top;"><#list requiredActions as reqActionItem>${msg("requiredAction." + reqActionItem)}<#sep><br></#list></td></tr>
</table>
</#if>
</@layout.emailLayout>
