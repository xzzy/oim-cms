# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2016-01-01 08:57
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracking', '0004_auto_20151221_1551'),
    ]

    operations = [
        migrations.AlterField(
            model_name='departmentuser',
            name='employee_id',
            field=models.CharField(blank=True, help_text="HR Employee ID, use 'n/a' if a contractor", max_length=128, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name='departmentuser',
            name='title',
            field=models.CharField(help_text='Staff position', max_length=128, null=True),
        ),
    ]
