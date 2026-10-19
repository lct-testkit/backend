<#import "template.ftl" as layout>
<@layout.emailLayout heading=msg("emailTestHeading") preheader=msg("emailTestPreheader") note=msg("emailTestNote")>
<p style="margin:0 0 12px 0;">${msg("emailTestLead")}</p>
</@layout.emailLayout>
